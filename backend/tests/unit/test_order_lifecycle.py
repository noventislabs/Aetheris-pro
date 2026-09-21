"""The order lifecycle, identity, and the crash window.

The crash-window tests are the reason phase 8a exists. They simulate the one
scenario that cannot be made safe by being careful at the call site:

    order record written      <- a crash here is safe
    venue submission          <- a crash HERE is the problem
    venue response            <- a lost response is the same problem

and assert that the system's answer is always "I do not know" rather than a
guess in either direction. Guessing it failed duplicates a position; guessing it
succeeded invents one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aetheris.core.errors import RiskRejectionCode
from aetheris.domain.enums import OrderSide, OrderState, OrderType, TradingMode
from aetheris.domain.order import (
    OrderFill,
    OrderIntent,
    OrderOrigin,
    ReconciliationAction,
    VenueOrderView,
)
from aetheris.engines.order.engine import OrderLifecycleEngine
from aetheris.engines.order.identity import (
    VENUE_ID_MAX_LENGTH,
    build_intent_key,
    client_order_id,
    is_valid_venue_id,
)
from aetheris.engines.order.machine import IllegalTransitionError
from aetheris.engines.order.store import (
    DuplicateClientOrderIdError,
    InMemoryOrderRepository,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(seconds=30)


def intent(**overrides: object) -> OrderIntent:
    base: dict[str, object] = {
        "account_id": "acct-1",
        "symbol": "ETHUSDT",
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "quantity": Decimal("1.0"),
        "mode": TradingMode.TESTNET,
        "origin": OrderOrigin.MANUAL,
        "intent_key": build_intent_key(
            symbol="ETHUSDT", side=OrderSide.BUY, purpose="manual", bucket="bucket-1"
        ),
        "created_at": NOW,
    }
    base.update(overrides)
    return OrderIntent(**base)  # type: ignore[arg-type]


def engine() -> OrderLifecycleEngine:
    return OrderLifecycleEngine(InMemoryOrderRepository())


async def submitted(eng: OrderLifecycleEngine, **kw: object):
    """An order taken right up to the edge of the uncertainty window."""
    record = await eng.create(intent(**kw))
    await eng.mark_validating(record.order_id, now=NOW)
    return await eng.mark_submitting(record.order_id, now=NOW)


# ----------------------------------------------------------------------
# Identity: derivable, bounded, venue-acceptable
# ----------------------------------------------------------------------


async def test_identity_is_derived_not_generated() -> None:
    """The property recovery depends on: same intent, same id, forever.

    A generated id is lost with the process, and an order whose identity cannot
    be reconstructed is an order that cannot be asked about.
    """
    key = build_intent_key(symbol="ETHUSDT", side=OrderSide.BUY, purpose="auto", bucket="169")
    first = client_order_id(account_id="acct-1", intent_key=key)
    second = client_order_id(account_id="acct-1", intent_key=key)
    assert first == second


async def test_identity_separates_account_symbol_side_and_bucket() -> None:
    def build(account: str, symbol: str, side: OrderSide, bucket: str) -> str:
        return client_order_id(
            account_id=account,
            intent_key=build_intent_key(symbol=symbol, side=side, purpose="auto", bucket=bucket),
        )

    base = build("acct-1", "ETHUSDT", OrderSide.BUY, "169")
    assert base != build("acct-2", "ETHUSDT", OrderSide.BUY, "169")
    assert base != build("acct-1", "BTCUSDT", OrderSide.BUY, "169")
    assert base != build("acct-1", "ETHUSDT", OrderSide.SELL, "169")
    assert base != build("acct-1", "ETHUSDT", OrderSide.BUY, "170")


@pytest.mark.parametrize(
    "symbol",
    ["ETHUSDT", "BTCUSDT", "1000000MOGUSDT", "AVERYLONGSYMBOLNAMEUSDT", "1000BONKUSDT"],
)
async def test_identity_fits_the_venue_limit_for_any_symbol(symbol: str) -> None:
    """The phase 7 readable key overflows 36 characters for long symbols.

    That would have been discovered at the first real submission of an unusual
    instrument, which is the worst available moment.
    """
    generated = client_order_id(
        account_id="acct-1",
        intent_key=build_intent_key(
            symbol=symbol, side=OrderSide.BUY, purpose="auto", bucket="1789992000000"
        ),
    )
    assert len(generated) <= VENUE_ID_MAX_LENGTH
    assert is_valid_venue_id(generated)


async def test_the_readable_form_survives_on_the_record() -> None:
    """Nothing is lost by hashing the venue field."""
    record = await engine().create(intent())
    assert record.intent.intent_key == "manual:ETHUSDT:BUY:bucket-1"
    assert record.client_order_id.startswith("aeth-")


async def test_the_same_intent_cannot_be_stored_twice() -> None:
    eng = engine()
    await eng.create(intent())
    with pytest.raises(DuplicateClientOrderIdError):
        await eng.create(intent())


# ----------------------------------------------------------------------
# The crash window
# ----------------------------------------------------------------------


async def test_the_record_exists_before_the_order_does() -> None:
    """A crash before submission leaves something to reconcile against."""
    eng = engine()
    record = await eng.create(intent())
    assert record.state is OrderState.CREATED
    assert record.submitted_at is None
    assert not record.reached_venue
    assert await eng.repository.get_by_client_order_id(record.client_order_id) is not None


async def test_submission_is_stamped_before_the_call_not_after_it() -> None:
    """The signature of an order whose fate is unknown.

    ``submitted_at`` set with no ``venue_order_id`` is exactly the state a crash
    mid-flight leaves behind, and it is what lets recovery tell "never sent"
    from "sent, outcome unknown".
    """
    eng = engine()
    record = await submitted(eng)
    assert record.state is OrderState.SUBMITTED
    assert record.submitted_at == NOW
    assert record.venue_order_id is None
    assert record.reached_venue


async def test_a_lost_response_becomes_unknown_not_a_guess() -> None:
    eng = engine()
    record = await submitted(eng)
    record = await eng.mark_unknown(record.order_id, reason="response never arrived", now=LATER)
    assert record.state is OrderState.UNKNOWN
    assert record.is_unreconciled


@pytest.mark.parametrize("guess", [OrderState.FILLED, OrderState.CANCELLED])
async def test_an_unknown_order_cannot_be_guessed_into_a_resolution(guess: OrderState) -> None:
    """The two inferences that would be most tempting, and most wrong."""
    eng = engine()
    record = await submitted(eng)
    record = await eng.mark_unknown(record.order_id, reason="silence", now=LATER)
    with pytest.raises(IllegalTransitionError):
        await eng.apply_venue_ack(record.order_id, venue_order_id="v-1", state=guess, now=LATER)


async def test_recovery_can_find_the_order_by_its_derived_identity() -> None:
    """What a restart actually does: re-derive the id, then ask the store."""
    eng = engine()
    record = await submitted(eng)

    rederived = client_order_id(account_id="acct-1", intent_key=record.intent.intent_key)
    assert rederived == record.client_order_id
    found = await eng.repository.get_by_client_order_id(rederived)
    assert found is not None
    assert found.order_id == record.order_id


async def test_every_non_terminal_order_is_asked_about_on_recovery() -> None:
    """Not only the explicitly unknown ones.

    An order believed ACCEPTED may have filled while the process was down, and
    assuming otherwise is the same error as assuming an unknown order never
    landed.
    """
    eng = engine()
    accepted = await submitted(eng)
    await eng.apply_venue_ack(
        accepted.order_id,
        venue_order_id="v-1",
        state=OrderState.ACCEPTED,
        now=NOW,
    )
    unknown = await submitted(eng, intent_key="manual:ETHUSDT:BUY:bucket-2")
    await eng.mark_unknown(unknown.order_id, reason="silence", now=NOW)

    pending = await eng.orders_needing_reconciliation()
    assert {r.order_id for r in pending} == {accepted.order_id, unknown.order_id}


# ----------------------------------------------------------------------
# Reconciliation: the asymmetry of "not found"
# ----------------------------------------------------------------------


async def test_not_found_settles_an_order_that_never_left() -> None:
    """Absence is proof, but only here."""
    eng = engine()
    record = await eng.create(intent())
    await eng.mark_validating(record.order_id, now=NOW)

    record, decision = await eng.reconcile(record.order_id, None, venue_reachable=True, now=LATER)
    assert decision.action is ReconciliationAction.RESOLVE_NEVER_SUBMITTED
    assert record.state is OrderState.CANCELLED
    assert "never submitted" in decision.detail


async def test_not_found_does_not_settle_an_order_that_was_sent() -> None:
    """The case that must not be optimised away.

    Treating this absence as "it never landed" and resubmitting is how one
    order becomes two positions.
    """
    eng = engine()
    record = await submitted(eng)
    record = await eng.mark_unknown(record.order_id, reason="silence", now=NOW)
    record = await eng.begin_reconciliation(record.order_id, now=LATER)

    record, decision = await eng.reconcile(record.order_id, None, venue_reachable=True, now=LATER)
    assert decision.action is ReconciliationAction.REMAIN_UNKNOWN
    assert record.state is OrderState.UNKNOWN
    assert "not proof" in decision.detail


async def test_an_unreachable_venue_changes_nothing() -> None:
    eng = engine()
    record = await submitted(eng)
    record = await eng.mark_unknown(record.order_id, reason="silence", now=NOW)
    before = record.state

    record, decision = await eng.reconcile(record.order_id, None, venue_reachable=False, now=LATER)
    assert decision.action is ReconciliationAction.VENUE_UNREACHABLE
    assert record.state is before
    assert record.last_reconciled_at == LATER


async def test_the_venue_is_the_authority_on_an_order_it_holds() -> None:
    eng = engine()
    record = await submitted(eng)
    record = await eng.mark_unknown(record.order_id, reason="silence", now=NOW)
    record = await eng.begin_reconciliation(record.order_id, now=LATER)

    view = VenueOrderView(
        client_order_id=record.client_order_id,
        venue_order_id="v-77",
        state=OrderState.FILLED,
        filled_quantity=Decimal("1.0"),
        observed_at=LATER,
    )
    record, decision = await eng.reconcile(record.order_id, view, venue_reachable=True, now=LATER)
    assert decision.action is ReconciliationAction.ADOPT_VENUE_STATE
    assert record.state is OrderState.FILLED
    assert record.venue_order_id == "v-77"
    assert record.filled_quantity == Decimal("1.0")


async def test_a_late_message_about_a_settled_order_is_recorded_not_applied() -> None:
    eng = engine()
    record = await submitted(eng)
    await eng.apply_venue_ack(
        record.order_id,
        venue_order_id="v-1",
        state=OrderState.CANCELLED,
        now=NOW,
    )

    view = VenueOrderView(
        client_order_id=record.client_order_id,
        state=OrderState.FILLED,
        filled_quantity=Decimal("1.0"),
        observed_at=LATER,
    )
    record, decision = await eng.reconcile(record.order_id, view, venue_reachable=True, now=LATER)
    assert decision.action is ReconciliationAction.RECORD_DISCREPANCY
    assert record.state is OrderState.CANCELLED, "a settled order is not reopened"
    assert len(record.discrepancies) == 1


async def test_fills_going_backwards_are_recorded_not_applied() -> None:
    eng = engine()
    record = await submitted(eng)
    await eng.apply_venue_ack(
        record.order_id,
        venue_order_id="v-1",
        state=OrderState.ACCEPTED,
        now=NOW,
    )
    await eng.apply_fill(
        record.order_id,
        OrderFill(fill_id="f-1", price=Decimal(100), quantity=Decimal("0.6"), filled_at=NOW),
        now=NOW,
    )

    view = VenueOrderView(
        client_order_id=record.client_order_id,
        state=OrderState.PARTIALLY_FILLED,
        filled_quantity=Decimal("0.2"),
        observed_at=LATER,
    )
    record, decision = await eng.reconcile(record.order_id, view, venue_reachable=True, now=LATER)
    assert decision.action is ReconciliationAction.RECORD_DISCREPANCY
    assert record.filled_quantity == Decimal("0.6")


async def test_reconciliation_attempts_are_counted() -> None:
    """A rising count with no resolution is itself a signal."""
    eng = engine()
    record = await submitted(eng)
    record = await eng.mark_unknown(record.order_id, reason="silence", now=NOW)
    record = await eng.begin_reconciliation(record.order_id, now=LATER)
    assert record.reconciliation_attempts == 1

    await eng.reconcile(record.order_id, None, venue_reachable=True, now=LATER)
    record = await eng.begin_reconciliation(record.order_id, now=LATER)
    assert record.reconciliation_attempts == 2


# ----------------------------------------------------------------------
# Fills
# ----------------------------------------------------------------------


async def test_a_partial_then_completing_fill_reaches_filled() -> None:
    eng = engine()
    record = await submitted(eng)
    await eng.apply_venue_ack(
        record.order_id,
        venue_order_id="v-1",
        state=OrderState.ACCEPTED,
        now=NOW,
    )
    record = await eng.apply_fill(
        record.order_id,
        OrderFill(fill_id="f-1", price=Decimal(100), quantity=Decimal("0.4"), filled_at=NOW),
        now=NOW,
    )
    assert record.state is OrderState.PARTIALLY_FILLED
    assert record.remaining_quantity == Decimal("0.6")

    record = await eng.apply_fill(
        record.order_id,
        OrderFill(fill_id="f-2", price=Decimal(110), quantity=Decimal("0.6"), filled_at=LATER),
        now=LATER,
    )
    assert record.state is OrderState.FILLED
    assert record.filled_quantity == Decimal("1.0")
    assert record.average_fill_price == Decimal("106.00000000")
    assert record.terminal_at == LATER


async def test_an_overfill_is_recorded_not_applied() -> None:
    eng = engine()
    record = await submitted(eng)
    await eng.apply_venue_ack(
        record.order_id,
        venue_order_id="v-1",
        state=OrderState.ACCEPTED,
        now=NOW,
    )
    record = await eng.apply_fill(
        record.order_id,
        OrderFill(fill_id="f-1", price=Decimal(100), quantity=Decimal("2.0"), filled_at=NOW),
        now=NOW,
    )
    assert record.filled_quantity == Decimal(0)
    assert record.state is OrderState.ACCEPTED
    assert len(record.discrepancies) == 1


# ----------------------------------------------------------------------
# Local cancellation, and manual resolution
# ----------------------------------------------------------------------


async def test_an_unsent_order_can_be_cancelled_locally() -> None:
    eng = engine()
    record = await eng.create(intent())
    record = await eng.cancel_before_submission(record.order_id, reason="changed mind", now=LATER)
    assert record.state is OrderState.CANCELLED


async def test_a_sent_order_cannot_be_cancelled_locally() -> None:
    """Abandoning it would leave an order at the venue nobody is tracking."""
    eng = engine()
    record = await submitted(eng)
    with pytest.raises(IllegalTransitionError) as caught:
        await eng.cancel_before_submission(record.order_id, reason="changed mind", now=LATER)
    assert "already been submitted" in str(caught.value)


async def test_a_human_can_settle_what_the_venue_will_not_and_it_is_recorded() -> None:
    """Necessary, and the one path that resolves without venue evidence.

    An order the venue never answers about would otherwise block entries
    forever, so it is narrow and loud rather than absent.
    """
    eng = engine()
    record = await submitted(eng)
    record = await eng.mark_unknown(record.order_id, reason="silence", now=NOW)

    record = await eng.resolve_manually(
        record.order_id,
        to_state=OrderState.CANCELLED,
        operator="asfaq",
        reason="confirmed absent in the venue UI",
        now=LATER,
    )
    assert record.state is OrderState.CANCELLED
    assert record.resolved_by_operator == "asfaq"
    assert "without venue evidence" in (record.reconciliation_detail or "")


async def test_manual_resolution_is_refused_for_an_order_progressing_normally() -> None:
    eng = engine()
    record = await submitted(eng)
    await eng.apply_venue_ack(
        record.order_id,
        venue_order_id="v-1",
        state=OrderState.ACCEPTED,
        now=NOW,
    )
    with pytest.raises(IllegalTransitionError) as caught:
        await eng.resolve_manually(
            record.order_id,
            to_state=OrderState.CANCELLED,
            operator="asfaq",
            reason="impatient",
            now=LATER,
        )
    assert "not awaiting reconciliation" in str(caught.value)


# ----------------------------------------------------------------------
# What the risk engine asks
# ----------------------------------------------------------------------


async def test_bookkeeping_counts_what_blocks_entry() -> None:
    eng = engine()
    clean = await submitted(eng)
    await eng.apply_venue_ack(
        clean.order_id,
        venue_order_id="v-1",
        state=OrderState.FILLED,
        now=NOW,
    )
    stuck = await submitted(eng, intent_key="manual:ETHUSDT:BUY:bucket-2")
    await eng.mark_unknown(stuck.order_id, reason="silence", now=NOW)

    books = await eng.bookkeeping()
    assert books.total == 2
    assert books.terminal == 1
    assert books.unreconciled == 1
    assert books.blocks_entry is True
    assert "awaiting reconciliation" in await eng.blocking_detail()


async def test_nothing_unreconciled_does_not_block() -> None:
    eng = engine()
    record = await submitted(eng)
    await eng.apply_venue_ack(
        record.order_id,
        venue_order_id="v-1",
        state=OrderState.FILLED,
        now=NOW,
    )
    assert (await eng.bookkeeping()).blocks_entry is False
    assert "No orders are awaiting" in await eng.blocking_detail()


# ----------------------------------------------------------------------
# Honesty about what this store can promise
# ----------------------------------------------------------------------


async def test_the_in_memory_store_does_not_claim_crash_recovery() -> None:
    """The protocol is built; the guarantee needs phase 8b.

    Order state that vanishes on restart loses the record of something that may
    exist at a venue -- which is precisely the record recovery depends on.
    """
    eng = engine()
    assert eng.repository.durable is False
    assert eng.supports_crash_recovery is False


async def test_a_rejected_order_keeps_its_risk_code() -> None:
    eng = engine()
    record = await eng.create(intent())
    await eng.mark_validating(record.order_id, now=NOW)
    record = await eng.mark_risk_rejected(
        record.order_id,
        code=RiskRejectionCode.MAX_LEVERAGE,
        detail="Leverage was not approved",
        now=NOW,
    )
    assert record.state is OrderState.REJECTED
    assert record.rejection_code is RiskRejectionCode.MAX_LEVERAGE
    assert record.is_terminal


# ----------------------------------------------------------------------
# The average price must move with the quantity
#
# Found in the phase 8a review. Adopting a venue total while keeping a locally
# computed average gave the record an implied notional that nothing observed --
# a fabricated price, arrived at by omission rather than invention.
# ----------------------------------------------------------------------


async def partially_filled(eng: OrderLifecycleEngine):
    record = await submitted(eng)
    await eng.apply_venue_ack(
        record.order_id,
        venue_order_id="v-1",
        state=OrderState.ACCEPTED,
        now=NOW,
    )
    return await eng.apply_fill(
        record.order_id,
        OrderFill(fill_id="f-1", price=Decimal(100), quantity=Decimal("0.6"), filled_at=NOW),
        now=NOW,
    )


async def test_adopting_a_venue_total_adopts_its_average_too() -> None:
    eng = engine()
    record = await partially_filled(eng)
    assert record.average_fill_price == Decimal("100.00000000")

    view = VenueOrderView(
        client_order_id=record.client_order_id,
        state=OrderState.FILLED,
        filled_quantity=Decimal("1.0"),
        average_fill_price=Decimal(105),
        observed_at=LATER,
    )
    record, _ = await eng.reconcile(record.order_id, view, venue_reachable=True, now=LATER)
    assert record.filled_quantity == Decimal("1.0")
    assert record.average_fill_price == Decimal(105)


async def test_a_venue_total_without_an_average_clears_the_stale_one() -> None:
    """``None`` is unknown. The previous value would be a price nothing observed."""
    eng = engine()
    record = await partially_filled(eng)

    view = VenueOrderView(
        client_order_id=record.client_order_id,
        state=OrderState.FILLED,
        filled_quantity=Decimal("1.0"),
        observed_at=LATER,
    )
    record, _ = await eng.reconcile(record.order_id, view, venue_reachable=True, now=LATER)
    assert record.filled_quantity == Decimal("1.0")
    assert record.average_fill_price is None


async def test_the_records_implied_notional_never_exceeds_what_was_observed() -> None:
    """The property the defect violated, stated directly."""
    eng = engine()
    record = await partially_filled(eng)
    view = VenueOrderView(
        client_order_id=record.client_order_id,
        state=OrderState.FILLED,
        filled_quantity=Decimal("1.0"),
        average_fill_price=Decimal(105),
        observed_at=LATER,
    )
    record, _ = await eng.reconcile(record.order_id, view, venue_reachable=True, now=LATER)
    implied = record.filled_quantity * (record.average_fill_price or Decimal(0))
    assert implied == Decimal("1.0") * Decimal(105)
