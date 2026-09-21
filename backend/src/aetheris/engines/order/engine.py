"""Order lifecycle orchestration.

Drives the state machine over a repository. Pure: no HTTP, no framework, no
venue, no clock of its own -- ``now`` is always passed in, so a test can put an
order through a crash window without waiting for one.

The method names describe the lifecycle rather than the transport, which is
what lets phase 8c attach a venue adapter without this file learning what a
venue is. In particular ``mark_submitting`` is called **before** the network
call and ``apply_venue_ack`` after it, and the gap between them is the
uncertainty window the whole design exists to survive.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from aetheris.core.errors import RiskRejectionCode
from aetheris.core.money import quantize_usdt
from aetheris.domain.enums import OrderState
from aetheris.domain.order import (
    OrderBookkeeping,
    OrderDiscrepancy,
    OrderFill,
    OrderIntent,
    OrderRecord,
    ReconciliationAction,
    ReconciliationDecision,
    VenueOrderView,
)
from aetheris.engines.order.identity import client_order_id
from aetheris.engines.order.machine import IllegalTransitionError, check_transition
from aetheris.engines.order.reconcile import blocking_orders, decide, summarise
from aetheris.engines.order.store import OrderRepository

__all__ = ["OrderLifecycleEngine"]


class OrderLifecycleEngine:
    """Owns the lifecycle of every order this system is responsible for."""

    def __init__(self, repository: OrderRepository) -> None:
        self._repository = repository
        self._sequence = 0

    @property
    def repository(self) -> OrderRepository:
        return self._repository

    @property
    def supports_crash_recovery(self) -> bool:
        """Whether a restart can recover anything.

        The protocol is implemented either way; the *guarantee* needs a durable
        store, and claiming it without one would be the same class of untruth
        the capability registry exists to prevent.
        """
        return self._repository.durable

    def _next(self, prefix: str) -> str:
        self._sequence += 1
        return f"{prefix}-{self._sequence}"

    # ------------------------------------------------------------------
    # Creation and the risk gate
    # ------------------------------------------------------------------

    def create(self, intent: OrderIntent) -> OrderRecord:
        """Write the intent down before anything else happens.

        This is the record a crash in the uncertainty window leaves behind. It
        exists before the order does, which is the only ordering that makes the
        order recoverable.
        """
        record = OrderRecord(
            order_id=self._next("order"),
            client_order_id=client_order_id(
                account_id=intent.account_id, intent_key=intent.intent_key
            ),
            intent=intent,
            state=OrderState.CREATED,
            created_at=intent.created_at,
            updated_at=intent.created_at,
        )
        return self._repository.add(record)

    def mark_validating(self, order_id: str, *, now: datetime) -> OrderRecord:
        """Enter the risk gate. Submission is unreachable without passing here."""
        return self._move(order_id, OrderState.VALIDATING, now=now)

    def mark_risk_rejected(
        self, order_id: str, *, code: RiskRejectionCode, detail: str, now: datetime
    ) -> OrderRecord:
        record = self._require(order_id)
        check_transition(record.state, OrderState.REJECTED, order_id=order_id)
        return self._repository.update(
            record.model_copy(
                update={
                    "state": OrderState.REJECTED,
                    "rejection_code": code,
                    "rejection_detail": detail,
                    "updated_at": now,
                    "terminal_at": now,
                }
            )
        )

    def cancel_before_submission(self, order_id: str, *, reason: str, now: datetime) -> OrderRecord:
        """Cancel something that never left. Always safe, by definition."""
        record = self._require(order_id)
        if record.reached_venue:
            raise IllegalTransitionError(
                f"{order_id} has already been submitted; cancelling it locally would "
                "abandon an order that may exist at the venue. Request a cancellation "
                "and reconcile instead."
            )
        check_transition(record.state, OrderState.CANCELLED, order_id=order_id)
        return self._repository.update(
            record.model_copy(
                update={
                    "state": OrderState.CANCELLED,
                    "rejection_detail": reason,
                    "updated_at": now,
                    "terminal_at": now,
                }
            )
        )

    # ------------------------------------------------------------------
    # The uncertainty window
    # ------------------------------------------------------------------

    def mark_submitting(self, order_id: str, *, now: datetime) -> OrderRecord:
        """Stamp the record **before** the submission leaves.

        Called immediately before the network call, never after it. A record
        with ``submitted_at`` set and no ``venue_order_id`` is exactly what a
        crash mid-flight leaves behind, and that pairing is what lets recovery
        tell "never sent" from "sent, outcome unknown" -- the difference
        between safely resolving an order and accidentally duplicating a
        position.
        """
        return self._move(order_id, OrderState.SUBMITTED, now=now, submitted_at=now)

    def apply_venue_ack(
        self,
        order_id: str,
        *,
        venue_order_id: str,
        state: OrderState,
        now: datetime,
    ) -> OrderRecord:
        """Record what the venue said when it accepted or refused the order."""
        record = self._require(order_id)
        check_transition(record.state, state, order_id=order_id)
        update: dict[str, object] = {
            "state": state,
            "venue_order_id": venue_order_id,
            "updated_at": now,
        }
        if state.is_terminal:
            update["terminal_at"] = now
        return self._repository.update(record.model_copy(update=update))

    def mark_unknown(self, order_id: str, *, reason: str, now: datetime) -> OrderRecord:
        """The honest state when the venue has not said.

        Not an error state. An order here is not lost -- it is unresolved, and
        the system keeps trading blocked until it is resolved by evidence.
        """
        record = self._require(order_id)
        check_transition(record.state, OrderState.UNKNOWN, order_id=order_id)
        return self._repository.update(
            record.model_copy(
                update={
                    "state": OrderState.UNKNOWN,
                    "reconciliation_detail": reason,
                    "updated_at": now,
                }
            )
        )

    # ------------------------------------------------------------------
    # Fills
    # ------------------------------------------------------------------

    def apply_fill(self, order_id: str, fill: OrderFill, *, now: datetime) -> OrderRecord:
        """Record an execution, or record why it could not be believed.

        A fill that would take the total backwards, or past the quantity
        ordered, is a discrepancy: it is written down and **not** applied. An
        order whose fills disagree with its own total cannot be reconciled
        against anything.
        """
        record = self._require(order_id)
        new_total = record.filled_quantity + fill.quantity

        if new_total > record.intent.quantity:
            return self._record_discrepancy(
                record,
                detail=(
                    f"A fill of {fill.quantity} would take {order_id} to {new_total}, past "
                    f"the {record.intent.quantity} ordered. Recorded, not applied."
                ),
                now=now,
            )

        target = (
            OrderState.FILLED
            if new_total == record.intent.quantity
            else OrderState.PARTIALLY_FILLED
        )
        check_transition(record.state, target, order_id=order_id)

        fills = (*record.fills, fill)
        notional = sum((f.price * f.quantity for f in fills), Decimal(0))
        average = quantize_usdt(notional / new_total) if new_total > 0 else None
        update: dict[str, object] = {
            "state": target,
            "fills": fills,
            "filled_quantity": new_total,
            "average_fill_price": average,
            "updated_at": now,
        }
        if target.is_terminal:
            update["terminal_at"] = now
        return self._repository.update(record.model_copy(update=update))

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def begin_reconciliation(self, order_id: str, *, now: datetime) -> OrderRecord:
        """Declare that this order is being asked about.

        The only exit from ``UNKNOWN``. Making it an explicit step is what
        stops an unknown order being resolved by anything other than an answer.
        """
        record = self._require(order_id)
        check_transition(record.state, OrderState.RECONCILING, order_id=order_id)
        return self._repository.update(
            record.model_copy(
                update={
                    "state": OrderState.RECONCILING,
                    "reconciliation_attempts": record.reconciliation_attempts + 1,
                    "updated_at": now,
                }
            )
        )

    def reconcile(
        self,
        order_id: str,
        venue_view: VenueOrderView | None,
        *,
        venue_reachable: bool,
        now: datetime,
    ) -> tuple[OrderRecord, ReconciliationDecision]:
        """Ask about one order and apply whatever the answer permits."""
        record = self._require(order_id)
        decision = decide(record, venue_view, venue_reachable=venue_reachable)
        return self.apply_reconciliation(order_id, decision, now=now), decision

    def apply_reconciliation(
        self, order_id: str, decision: ReconciliationDecision, *, now: datetime
    ) -> OrderRecord:
        """Apply a decision. The single auditable step that changes state."""
        record = self._require(order_id)

        if decision.action is ReconciliationAction.RECORD_DISCREPANCY:
            return self._record_discrepancy(
                record, detail=decision.discrepancy or decision.detail, now=now
            )

        if decision.action is ReconciliationAction.VENUE_UNREACHABLE:
            return self._repository.update(
                record.model_copy(
                    update={
                        "reconciliation_detail": decision.detail,
                        "last_reconciled_at": now,
                        "updated_at": now,
                    }
                )
            )

        target = decision.target_state
        if target is None:  # pragma: no cover - the remaining actions all set one
            return record

        if target is record.state:
            # REMAIN_UNKNOWN on an order already UNKNOWN. Nothing moves; the
            # attempt and its reason are still recorded, because a rising
            # attempt count with no resolution is itself worth seeing.
            return self._repository.update(
                record.model_copy(
                    update={
                        "reconciliation_detail": decision.detail,
                        "last_reconciled_at": now,
                        "updated_at": now,
                    }
                )
            )

        check_transition(record.state, target, order_id=order_id)
        update: dict[str, object] = {
            "state": target,
            "reconciliation_detail": decision.detail,
            "last_reconciled_at": now,
            "updated_at": now,
        }
        if decision.venue_order_id is not None:
            update["venue_order_id"] = decision.venue_order_id
        if decision.filled_quantity is not None:
            update["filled_quantity"] = decision.filled_quantity
            # The venue is the authority on its own totals, but our per-fill
            # detail cannot be reconstructed from a summary. Dropping the fills
            # keeps the record internally consistent rather than letting the
            # parts disagree with the total.
            update["fills"] = ()
            # The average must move with the quantity. Keeping a locally
            # computed average beside an adopted venue total would give the
            # record an implied notional that nothing ever observed -- a
            # fabricated price, arrived at by omission. When the venue supplies
            # no average the honest value is None: unknown, not stale.
            update["average_fill_price"] = decision.average_fill_price
        if target.is_terminal:
            update["terminal_at"] = now
        return self._repository.update(record.model_copy(update=update))

    def resolve_manually(
        self,
        order_id: str,
        *,
        to_state: OrderState,
        operator: str,
        reason: str,
        now: datetime,
    ) -> OrderRecord:
        """Let a human settle what the venue could not.

        Necessary: an order the venue will never answer about would otherwise
        block entries forever. Dangerous: it is the one path that resolves an
        order without venue evidence.

        So it is narrow and loud. Only from ``UNKNOWN`` or ``RECONCILING``, only
        to a state reconciliation could have concluded, and always recorded with
        who did it and why -- a manual resolution that left no trace would be
        indistinguishable from the inference this whole design forbids.
        """
        record = self._require(order_id)
        if not record.is_unreconciled:
            raise IllegalTransitionError(
                f"{order_id} is {record.state.value}, not awaiting reconciliation. Manual "
                "resolution exists for orders the venue cannot settle, not as a way to "
                "move an order that is progressing normally."
            )
        if record.state is OrderState.UNKNOWN:
            record = self.begin_reconciliation(order_id, now=now)
        check_transition(record.state, to_state, order_id=order_id)
        update: dict[str, object] = {
            "state": to_state,
            "resolved_by_operator": operator,
            "operator_reason": reason,
            "reconciliation_detail": (
                f"Resolved manually by {operator} without venue evidence: {reason}"
            ),
            "last_reconciled_at": now,
            "updated_at": now,
        }
        if to_state.is_terminal:
            update["terminal_at"] = now
        return self._repository.update(record.model_copy(update=update))

    # ------------------------------------------------------------------
    # What the risk engine asks
    # ------------------------------------------------------------------

    def bookkeeping(self) -> OrderBookkeeping:
        records = self._repository.all_records()
        return OrderBookkeeping(
            total=len(records),
            open_orders=sum(1 for r in records if r.state.is_open),
            unreconciled=len(blocking_orders(records)),
            terminal=sum(1 for r in records if r.is_terminal),
            discrepancies=sum(len(r.discrepancies) for r in records),
        )

    def unreconciled_orders(self) -> tuple[OrderRecord, ...]:
        return blocking_orders(self._repository.all_records())

    def blocking_detail(self) -> str:
        return summarise(self._repository.all_records())

    def orders_needing_reconciliation(self) -> tuple[OrderRecord, ...]:
        """Every non-terminal order -- what a recovery pass must ask about.

        Not only the explicitly unknown ones: an order believed ``ACCEPTED``
        may have filled while the process was down, and assuming otherwise is
        the same error as assuming an unknown order never landed.
        """
        return self._repository.open_records()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require(self, order_id: str) -> OrderRecord:
        record = self._repository.get(order_id)
        if record is None:
            raise KeyError(f"No order {order_id}")
        return record

    def _move(
        self,
        order_id: str,
        target: OrderState,
        *,
        now: datetime,
        submitted_at: datetime | None = None,
    ) -> OrderRecord:
        record = self._require(order_id)
        check_transition(record.state, target, order_id=order_id)
        update: dict[str, object] = {"state": target, "updated_at": now}
        if submitted_at is not None:
            update["submitted_at"] = submitted_at
        if target.is_terminal:
            update["terminal_at"] = now
        return self._repository.update(record.model_copy(update=update))

    def _record_discrepancy(
        self, record: OrderRecord, *, detail: str, now: datetime
    ) -> OrderRecord:
        discrepancy = OrderDiscrepancy(observed_at=now, detail=detail, local_state=record.state)
        return self._repository.update(
            record.model_copy(
                update={
                    "discrepancies": (*record.discrepancies, discrepancy),
                    "last_reconciled_at": now,
                    "updated_at": now,
                }
            )
        )
