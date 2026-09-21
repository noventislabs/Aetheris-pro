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

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
from aetheris.engines.order.identity import client_order_id, new_order_id
from aetheris.engines.order.machine import IllegalTransitionError, check_transition
from aetheris.engines.order.reconcile import blocking_orders, decide, summarise
from aetheris.engines.order.store import OrderRepository

__all__ = ["OrderLifecycleEngine", "ReconciliationPending", "RecoveryReport"]


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """What a restart found, and what it did about it."""

    durable: bool
    scanned: int = 0
    #: Orders that had reached the venue and were still in flight. Moved to
    #: UNKNOWN, because the process died without learning their fate.
    interrupted: tuple[str, ...] = ()
    #: Orders that never left. Nothing to reconcile: absence at the venue is
    #: certain, not inferred.
    never_submitted: tuple[str, ...] = ()
    #: Already UNKNOWN or RECONCILING. Untouched, which is what makes a second
    #: run of recovery a no-op.
    already_unresolved: tuple[str, ...] = ()
    #: Orders that could not be read at all. Recorded as a discrepancy and
    #: counted as blocking.
    unreadable: tuple[str, ...] = ()

    @property
    def blocking(self) -> int:
        return len(self.interrupted) + len(self.already_unresolved) + len(self.unreadable)


@dataclass(frozen=True, slots=True)
class ReconciliationPending:
    """How many orders forbid a new entry, and why."""

    count: int
    detail: str


class OrderLifecycleEngine:
    """Owns the lifecycle of every order this system is responsible for."""

    def __init__(self, repository: OrderRepository) -> None:
        self._repository = repository

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

    # ------------------------------------------------------------------
    # Creation and the risk gate
    # ------------------------------------------------------------------

    async def create(self, intent: OrderIntent) -> OrderRecord:
        """Write the intent down before anything else happens.

        This is the record a crash in the uncertainty window leaves behind. It
        exists before the order does, which is the only ordering that makes the
        order recoverable.
        """
        return await self._repository.add(self._build(intent))

    async def create_or_get(self, intent: OrderIntent) -> tuple[OrderRecord, bool]:
        """Write the intent down, or return the order that already carries it.

        What a retry after a restart needs. Identity is derived from the
        intent, so the same intent produces the same ``client_order_id`` on
        every boot; raising on the second attempt would be technically correct
        and operationally useless, because the caller's real question is "what
        happened to my order", and the persisted record answers it.

        Returns ``(record, created)`` so a caller can tell a fresh order from a
        replay -- a replay must not be submitted to a venue again.
        """
        record = self._build(intent)
        return await self._repository.add_or_get(record)

    def _build(self, intent: OrderIntent) -> OrderRecord:
        return OrderRecord(
            order_id=new_order_id(),
            client_order_id=client_order_id(
                account_id=intent.account_id, intent_key=intent.intent_key
            ),
            intent=intent,
            state=OrderState.CREATED,
            created_at=intent.created_at,
            updated_at=intent.created_at,
        )

    async def mark_validating(self, order_id: str, *, now: datetime) -> OrderRecord:
        """Enter the risk gate. Submission is unreachable without passing here."""
        return await self._move(order_id, OrderState.VALIDATING, now=now)

    async def mark_risk_rejected(
        self, order_id: str, *, code: RiskRejectionCode, detail: str, now: datetime
    ) -> OrderRecord:
        async with self._held(order_id) as record:
            check_transition(record.state, OrderState.REJECTED, order_id=order_id)
            return await self._repository.update(
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

    async def cancel_before_submission(
        self, order_id: str, *, reason: str, now: datetime
    ) -> OrderRecord:
        """Cancel something that never left. Always safe, by definition."""
        async with self._held(order_id) as record:
            if record.reached_venue:
                raise IllegalTransitionError(
                    f"{order_id} has already been submitted; cancelling it locally would "
                    "abandon an order that may exist at the venue. Request a cancellation "
                    "and reconcile instead."
                )
            check_transition(record.state, OrderState.CANCELLED, order_id=order_id)
            return await self._repository.update(
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

    async def mark_submitting(self, order_id: str, *, now: datetime) -> OrderRecord:
        """Stamp the record **before** the submission leaves.

        Called immediately before the network call, never after it. A record
        with ``submitted_at`` set and no ``venue_order_id`` is exactly what a
        crash mid-flight leaves behind, and that pairing is what lets recovery
        tell "never sent" from "sent, outcome unknown" -- the difference
        between safely resolving an order and accidentally duplicating a
        position.
        """
        return await self._move(order_id, OrderState.SUBMITTED, now=now, submitted_at=now)

    async def apply_venue_ack(
        self,
        order_id: str,
        *,
        venue_order_id: str,
        state: OrderState,
        now: datetime,
    ) -> OrderRecord:
        """Record what the venue said when it accepted or refused the order."""
        async with self._held(order_id) as record:
            check_transition(record.state, state, order_id=order_id)
            update: dict[str, object] = {
                "state": state,
                "venue_order_id": venue_order_id,
                "updated_at": now,
            }
            if state.is_terminal:
                update["terminal_at"] = now
            return await self._repository.update(record.model_copy(update=update))

    async def mark_unknown(self, order_id: str, *, reason: str, now: datetime) -> OrderRecord:
        """The honest state when the venue has not said.

        Not an error state. An order here is not lost -- it is unresolved, and
        the system keeps trading blocked until it is resolved by evidence.
        """
        async with self._held(order_id) as record:
            check_transition(record.state, OrderState.UNKNOWN, order_id=order_id)
            return await self._repository.update(
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

    async def apply_fill(self, order_id: str, fill: OrderFill, *, now: datetime) -> OrderRecord:
        """Record an execution, or record why it could not be believed.

        A fill that would take the total backwards, or past the quantity
        ordered, is a discrepancy: it is written down and **not** applied. An
        order whose fills disagree with its own total cannot be reconciled
        against anything.
        """
        async with self._held(order_id) as record:
            new_total = record.filled_quantity + fill.quantity

            if new_total > record.intent.quantity:
                return await self._record_discrepancy(
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
            return await self._repository.update(record.model_copy(update=update))

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    async def begin_reconciliation(self, order_id: str, *, now: datetime) -> OrderRecord:
        """Declare that this order is being asked about.

        The only exit from ``UNKNOWN``. Making it an explicit step is what
        stops an unknown order being resolved by anything other than an answer.
        """
        async with self._held(order_id) as record:
            check_transition(record.state, OrderState.RECONCILING, order_id=order_id)
            return await self._repository.update(
                record.model_copy(
                    update={
                        "state": OrderState.RECONCILING,
                        "reconciliation_attempts": record.reconciliation_attempts + 1,
                        "updated_at": now,
                    }
                )
            )

    async def reconcile(
        self,
        order_id: str,
        venue_view: VenueOrderView | None,
        *,
        venue_reachable: bool,
        now: datetime,
    ) -> tuple[OrderRecord, ReconciliationDecision]:
        """Ask about one order and apply whatever the answer permits.

        Single-flight: the order is held for the read-decide-write sequence, so
        a second pass running concurrently waits rather than reaching its own
        conclusion about the same order. Two passes that both read ``UNKNOWN``
        and disagree would otherwise resolve by whichever wrote last, silently.
        """
        async with self._repository.locked(order_id) as held:
            if held is None:
                raise KeyError(f"No order {order_id}")
            decision = decide(held, venue_view, venue_reachable=venue_reachable)
            return await self.apply_reconciliation(order_id, decision, now=now), decision

    async def apply_reconciliation(
        self, order_id: str, decision: ReconciliationDecision, *, now: datetime
    ) -> OrderRecord:
        """Apply a decision. The single auditable step that changes state."""
        async with self._held(order_id) as record:
            if decision.action is ReconciliationAction.RECORD_DISCREPANCY:
                return await self._record_discrepancy(
                    record, detail=decision.discrepancy or decision.detail, now=now
                )

            if decision.action is ReconciliationAction.VENUE_UNREACHABLE:
                return await self._repository.update(
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
                return await self._repository.update(
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
            return await self._repository.update(record.model_copy(update=update))

    async def resolve_manually(
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
        async with self._held(order_id) as record:
            if not record.is_unreconciled:
                raise IllegalTransitionError(
                    f"{order_id} is {record.state.value}, not awaiting reconciliation. Manual "
                    "resolution exists for orders the venue cannot settle, not as a way to "
                    "move an order that is progressing normally."
                )
            if record.state is OrderState.UNKNOWN:
                record = await self.begin_reconciliation(order_id, now=now)
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
            return await self._repository.update(record.model_copy(update=update))

    # ------------------------------------------------------------------
    # What the risk engine asks
    # ------------------------------------------------------------------

    async def bookkeeping(self) -> OrderBookkeeping:
        records = await self._repository.all_records()
        return OrderBookkeeping(
            total=len(records),
            open_orders=sum(1 for r in records if r.state.is_open),
            unreconciled=len(blocking_orders(records)),
            terminal=sum(1 for r in records if r.is_terminal),
            discrepancies=sum(len(r.discrepancies) for r in records),
        )

    async def unreconciled_orders(self) -> tuple[OrderRecord, ...]:
        return blocking_orders(await self._repository.all_records())

    async def blocking_detail(self) -> str:
        return summarise(await self._repository.all_records())

    async def reconciliation_pending(self) -> ReconciliationPending:
        """The real count the risk engine asks for.

        Read from durable state rather than assumed. Unreadable orders are
        counted as blocking: not knowing what an order is cannot count for less
        than knowing it is unreconciled.
        """
        scan = await self._repository.scan_open()
        if scan.blocking == 0:
            return ReconciliationPending(0, "No orders are awaiting reconciliation.")
        detail = summarise(scan.records)
        if scan.unreadable:
            detail = (
                f"{len(scan.unreadable)} stored order(s) could not be read and may "
                "correspond to a position at a venue. "
            ) + detail
        return ReconciliationPending(scan.blocking, detail)

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    async def recover(self, *, now: datetime) -> RecoveryReport:
        """Bring persisted orders back to an honest state after a restart.

        **Nothing here contacts a venue and nothing here guesses.** The only
        move it makes is from "in flight" to ``UNKNOWN``, which is a statement
        about this system -- we were interrupted and do not know -- rather than
        a claim about the order. Resolving it needs evidence, and evidence
        needs the venue adapter that phase 8c brings.

        The distinction that makes this safe is ``reached_venue``. An order
        still in ``CREATED`` or ``VALIDATING`` never left, so its absence at a
        venue is certain rather than inferred, and moving it to ``UNKNOWN``
        would manufacture doubt the machine would then refuse to resolve
        without evidence that cannot exist. Those are reported and left alone.

        Idempotent. A second run sees the same orders already ``UNKNOWN`` and
        does nothing, so a crash *during* recovery is survivable too.
        """
        if not self._repository.durable:
            # Nothing survived to recover. Saying so is the point: a report of
            # zero interrupted orders from a store that forgets everything
            # looks identical to a clean restart, and is not one.
            return RecoveryReport(durable=False)

        scan = await self._repository.scan_open()
        interrupted: list[str] = []
        never_submitted: list[str] = []
        already: list[str] = []

        for record in scan.records:
            if record.is_unreconciled:
                already.append(record.order_id)
                continue
            if not record.reached_venue:
                never_submitted.append(record.order_id)
                continue
            await self.mark_unknown(
                record.order_id,
                reason=(
                    "The process stopped while this order was in flight. Its state at "
                    "the venue was never confirmed, so it is unknown rather than assumed."
                ),
                now=now,
            )
            interrupted.append(record.order_id)

        for damaged in scan.unreadable:
            await self._repository.record_unreadable(
                damaged.order_id,
                detail=(
                    f"Stored order could not be loaded ({damaged.reason}) while its "
                    f"state was {damaged.state}. It is treated as blocking: an order "
                    "that cannot be read may still be a position at a venue."
                ),
                now=now,
            )

        return RecoveryReport(
            durable=True,
            scanned=len(scan.records) + len(scan.unreadable),
            interrupted=tuple(interrupted),
            never_submitted=tuple(never_submitted),
            already_unresolved=tuple(already),
            unreadable=tuple(d.order_id for d in scan.unreadable),
        )

    async def orders_needing_reconciliation(self) -> tuple[OrderRecord, ...]:
        """Every non-terminal order -- what a recovery pass must ask about.

        Not only the explicitly unknown ones: an order believed ``ACCEPTED``
        may have filled while the process was down, and assuming otherwise is
        the same error as assuming an unknown order never landed.
        """
        return await self._repository.open_records()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _require(self, order_id: str) -> OrderRecord:
        record = await self._repository.get(order_id)
        if record is None:
            raise KeyError(f"No order {order_id}")
        return record

    @asynccontextmanager
    async def _held(self, order_id: str) -> AsyncIterator[OrderRecord]:
        """Read an order under a row lock and keep it locked while it changes.

        Every mutator here is a read-modify-write: it reads the state, checks
        the transition against it, and writes a state derived from it. Split
        across two transactions -- which is what an unlocked read followed by a
        write is -- two callers can both validate against the same old state
        and both write, and the second silently erases the first. For
        ``apply_fill`` that is not merely a lost update: the fill rows both
        land while the total reflects one of them, and the record stops being
        readable at all.

        The lock is on one row and is held for the whole sequence. It is never
        held across a venue call: ``reconcile`` takes the venue's answer as an
        argument, so the network happens before the lock is taken.
        """
        async with self._repository.locked(order_id) as record:
            if record is None:
                raise KeyError(f"No order {order_id}")
            yield record

    async def _move(
        self,
        order_id: str,
        target: OrderState,
        *,
        now: datetime,
        submitted_at: datetime | None = None,
    ) -> OrderRecord:
        async with self._held(order_id) as record:
            check_transition(record.state, target, order_id=order_id)
            update: dict[str, object] = {"state": target, "updated_at": now}
            if submitted_at is not None:
                update["submitted_at"] = submitted_at
            if target.is_terminal:
                update["terminal_at"] = now
            return await self._repository.update(record.model_copy(update=update))

    async def _record_discrepancy(
        self, record: OrderRecord, *, detail: str, now: datetime
    ) -> OrderRecord:
        discrepancy = OrderDiscrepancy(observed_at=now, detail=detail, local_state=record.state)
        return await self._repository.update(
            record.model_copy(
                update={
                    "discrepancies": (*record.discrepancies, discrepancy),
                    "last_reconciled_at": now,
                    "updated_at": now,
                }
            )
        )
