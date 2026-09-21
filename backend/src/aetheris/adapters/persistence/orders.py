"""Durable order storage.

This is the implementation that makes crash recovery a guarantee rather than a
protocol. It reports ``durable = True``, and the two properties that earns it
are both enforced by PostgreSQL rather than by this file:

**Idempotency is a unique constraint.** ``UNIQUE (account_id, client_order_id)``
holds when a recovery pass and a live request check-then-insert concurrently,
which no in-process structure can. The integrity error is caught and read as
*confirmation the order already exists*, not as a failure -- because that is
what it means when identity is derived from intent.

**Reconciliation is single-flight.** ``locked()`` opens a transaction and takes
``SELECT ... FOR UPDATE`` on one row. A second worker blocks on that row rather
than reaching an independent conclusion about the same order. One row, not a
table lock: a global lock would serialise every order behind the slowest
reconciliation.

The session is held in a ``ContextVar`` so that a repository call made *inside*
a ``locked()`` block joins the open transaction instead of starting its own.
Without that, the lock would be released by the time the caller wrote its
update -- the sequence would look serialised and would not be.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aetheris.adapters.persistence.engine import DatabaseUnavailableError, session_scope
from aetheris.adapters.persistence.models import OrderDiscrepancyRow, OrderFillRow, OrderRow
from aetheris.core.errors import RiskRejectionCode
from aetheris.domain.enums import OrderSide, OrderState, OrderType, TradingMode
from aetheris.domain.order import (
    OrderDiscrepancy,
    OrderFill,
    OrderIntent,
    OrderOrigin,
    OrderRecord,
)
from aetheris.engines.order.store import DuplicateClientOrderIdError, OrderRepository

__all__ = ["PostgresOrderRepository"]

#: The transaction a nested repository call should join, if one is open.
_ambient: ContextVar[AsyncSession | None] = ContextVar("aetheris_order_session", default=None)


class PostgresOrderRepository(OrderRepository):
    """Order records in PostgreSQL, scoped to one owner and one account.

    The owner is taken in the constructor rather than per call. Phase 1's
    isolation requirement is that a repository method cannot be invoked without
    an owner -- making it a constructor argument is the strongest version of
    that: there is no method to call that omits it.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        owner_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> None:
        self._factory = session_factory
        self._owner_id = owner_id
        self._account_id = account_id

    @property
    def durable(self) -> bool:
        return True

    @property
    def account_id(self) -> uuid.UUID:
        return self._account_id

    # ------------------------------------------------------------------
    # Session handling
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        """Join the open transaction, or start one scoped to this owner."""
        existing = _ambient.get()
        if existing is not None:
            yield existing
            return
        async with session_scope(self._factory, str(self._owner_id)) as session:
            yield session

    @asynccontextmanager
    async def locked(self, order_id: str) -> AsyncIterator[OrderRecord | None]:
        """Take a row lock and hold it for the caller's whole block."""
        async with session_scope(self._factory, str(self._owner_id)) as session:
            token = _ambient.set(session)
            try:
                row = (
                    await session.execute(
                        select(OrderRow)
                        .where(
                            OrderRow.order_id == order_id,
                            OrderRow.account_id == self._account_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                record = await self._hydrate(session, row) if row is not None else None
                yield record
            finally:
                _ambient.reset(token)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def add(self, record: OrderRecord) -> OrderRecord:
        self._require_own_account(record)
        try:
            async with self._session() as session:
                row = OrderRow(
                    owner_id=self._owner_id,
                    account_id=self._account_id,
                    **self._columns(record),
                )
                session.add(row)
                await session.flush()
                await self._write_children(session, row, record)
                return record
        except IntegrityError as exc:
            raise self._integrity(record, exc) from None
        except SQLAlchemyError as exc:
            raise DatabaseUnavailableError(f"could not store order: {type(exc).__name__}") from None

    async def update(self, record: OrderRecord) -> OrderRecord:
        self._require_own_account(record)
        try:
            async with self._session() as session:
                row = await self._row(session, record.order_id)
                if row is None:
                    raise KeyError(f"No order {record.order_id} to update")
                for key, value in self._columns(record).items():
                    setattr(row, key, value)
                await session.flush()
                await self._write_children(session, row, record)
                return record
        except IntegrityError as exc:
            raise self._integrity(record, exc) from None
        except SQLAlchemyError as exc:
            raise DatabaseUnavailableError(
                f"could not update order: {type(exc).__name__}"
            ) from None

    async def _write_children(
        self, session: AsyncSession, row: OrderRow, record: OrderRecord
    ) -> None:
        """Append fills and discrepancies in the caller's transaction.

        Both are append-only from this side: a fill already stored is not
        rewritten, and a discrepancy is evidence. Writing them here rather than
        in a second call is transaction boundary C -- the fill and the order
        total that reflects it commit together or not at all, because separately
        they can diverge and the record's own validator would then reject what
        it just loaded.
        """
        known_fills = set(
            (
                await session.execute(
                    select(OrderFillRow.fill_id).where(OrderFillRow.order_id == row.id)
                )
            )
            .scalars()
            .all()
        )
        for fill in record.fills:
            if fill.fill_id in known_fills:
                continue
            session.add(
                OrderFillRow(
                    owner_id=self._owner_id,
                    order_id=row.id,
                    fill_id=fill.fill_id,
                    venue_trade_id=fill.venue_trade_id,
                    price=fill.price,
                    quantity=fill.quantity,
                    fee=fill.fee,
                    filled_at=fill.filled_at,
                )
            )

        stored = (
            await session.execute(
                select(OrderDiscrepancyRow.observed_at, OrderDiscrepancyRow.detail).where(
                    OrderDiscrepancyRow.order_id == row.id
                )
            )
        ).all()
        seen = {(observed_at, detail) for observed_at, detail in stored}
        for discrepancy in record.discrepancies:
            if (discrepancy.observed_at, discrepancy.detail) in seen:
                continue
            session.add(
                OrderDiscrepancyRow(
                    owner_id=self._owner_id,
                    order_id=row.id,
                    observed_at=discrepancy.observed_at,
                    detail=discrepancy.detail,
                    local_state=discrepancy.local_state.value,
                    venue_state=(
                        discrepancy.venue_state.value if discrepancy.venue_state else None
                    ),
                )
            )
        await session.flush()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get(self, order_id: str) -> OrderRecord | None:
        async with self._session() as session:
            row = await self._row(session, order_id)
            return await self._hydrate(session, row) if row is not None else None

    async def get_by_client_order_id(self, client_order_id: str) -> OrderRecord | None:
        async with self._session() as session:
            row = (
                await session.execute(
                    select(OrderRow).where(
                        OrderRow.client_order_id == client_order_id,
                        OrderRow.account_id == self._account_id,
                    )
                )
            ).scalar_one_or_none()
            return await self._hydrate(session, row) if row is not None else None

    async def all_records(self) -> tuple[OrderRecord, ...]:
        async with self._session() as session:
            rows = (
                (
                    await session.execute(
                        select(OrderRow)
                        .where(OrderRow.account_id == self._account_id)
                        .order_by(OrderRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return tuple([await self._hydrate(session, row) for row in rows])

    async def open_records(self) -> tuple[OrderRecord, ...]:
        terminal = [state.value for state in OrderState if state.is_terminal]
        async with self._session() as session:
            rows = (
                (
                    await session.execute(
                        select(OrderRow)
                        .where(
                            OrderRow.account_id == self._account_id,
                            OrderRow.state.notin_(terminal),
                        )
                        .order_by(OrderRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return tuple([await self._hydrate(session, row) for row in rows])

    async def clear(self) -> None:
        """Discard this account's orders. Development and tests only."""
        async with self._session() as session:
            rows = (
                (
                    await session.execute(
                        select(OrderRow).where(OrderRow.account_id == self._account_id)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                await session.delete(row)
            await session.flush()

    # ------------------------------------------------------------------
    # Mapping
    # ------------------------------------------------------------------

    def _require_own_account(self, record: OrderRecord) -> None:
        """An order belongs to the account this repository was built for.

        Row-level security already stops one owner reading another's rows. This
        is the complementary check on the write side, caught in the application
        so the failure names the cause rather than surfacing as a policy
        violation from the driver.
        """
        try:
            declared = uuid.UUID(record.intent.account_id)
        except ValueError:
            raise ValueError(
                f"order {record.order_id} names account {record.intent.account_id!r}, "
                "which is not an account identifier this store recognises"
            ) from None
        if declared != self._account_id:
            raise ValueError(
                f"order {record.order_id} belongs to a different account than this "
                "repository was opened for"
            )

    def _integrity(self, record: OrderRecord, exc: IntegrityError) -> Exception:
        """Read which constraint fired, and say what it means.

        The driver's exception is wrapped twice -- SQLAlchemy around a DBAPI
        shim around asyncpg -- so ``constraint_name`` is looked for down the
        ``__cause__`` chain rather than on the outermost object. The text is
        searched only as a fallback, because a message is not an API.
        """
        constraint = _constraint_name(exc)
        if constraint == "uq_orders_account_client_id" or (
            not constraint and "uq_orders_account_client_id" in str(exc)
        ):
            return DuplicateClientOrderIdError(
                f"An order with client id {record.client_order_id} already exists. "
                "Identity is derived from intent, so a duplicate means the same intent "
                "was submitted twice -- which is exactly what the identity exists to "
                "prevent."
            )
        return DatabaseUnavailableError(f"integrity constraint violated: {constraint or 'unknown'}")

    async def _row(self, session: AsyncSession, order_id: str) -> OrderRow | None:
        return (
            await session.execute(
                select(OrderRow).where(
                    OrderRow.order_id == order_id,
                    OrderRow.account_id == self._account_id,
                )
            )
        ).scalar_one_or_none()

    def _columns(self, record: OrderRecord) -> dict[str, Any]:
        intent = record.intent
        return {
            "order_id": record.order_id,
            "client_order_id": record.client_order_id,
            "venue_order_id": record.venue_order_id,
            "state": record.state.value,
            "symbol": intent.symbol,
            "side": intent.side.value,
            "order_type": intent.order_type.value,
            "mode": intent.mode.value,
            "origin": intent.origin.value,
            "intent_key": intent.intent_key,
            "reduce_only": intent.reduce_only,
            "quantity": intent.quantity,
            "price": intent.price,
            "filled_quantity": record.filled_quantity,
            "average_fill_price": record.average_fill_price,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "submitted_at": record.submitted_at,
            "terminal_at": record.terminal_at,
            "rejection_code": record.rejection_code.value if record.rejection_code else None,
            "rejection_detail": record.rejection_detail,
            "venue_rejection": record.venue_rejection,
            "reconciliation_attempts": record.reconciliation_attempts,
            "last_reconciled_at": record.last_reconciled_at,
            "reconciliation_detail": record.reconciliation_detail,
            "resolved_by_operator": record.resolved_by_operator,
            "operator_reason": record.operator_reason,
        }

    async def _hydrate(self, session: AsyncSession, row: OrderRow) -> OrderRecord:
        fills = (
            (
                await session.execute(
                    select(OrderFillRow)
                    .where(OrderFillRow.order_id == row.id)
                    .order_by(OrderFillRow.filled_at, OrderFillRow.fill_id)
                )
            )
            .scalars()
            .all()
        )
        discrepancies = (
            (
                await session.execute(
                    select(OrderDiscrepancyRow)
                    .where(OrderDiscrepancyRow.order_id == row.id)
                    .order_by(OrderDiscrepancyRow.observed_at)
                )
            )
            .scalars()
            .all()
        )
        return OrderRecord(
            order_id=row.order_id,
            client_order_id=row.client_order_id,
            venue_order_id=row.venue_order_id,
            intent=OrderIntent(
                account_id=str(row.account_id),
                symbol=row.symbol,
                side=OrderSide(row.side),
                order_type=OrderType(row.order_type),
                quantity=row.quantity,
                price=row.price,
                reduce_only=row.reduce_only,
                mode=TradingMode(row.mode),
                origin=OrderOrigin(row.origin),
                intent_key=row.intent_key,
                created_at=_utc(row.created_at),
            ),
            state=OrderState(row.state),
            filled_quantity=row.filled_quantity,
            average_fill_price=row.average_fill_price,
            fills=tuple(
                OrderFill(
                    fill_id=f.fill_id,
                    price=f.price,
                    quantity=f.quantity,
                    fee=f.fee,
                    filled_at=_utc(f.filled_at),
                    venue_trade_id=f.venue_trade_id,
                )
                for f in fills
            ),
            created_at=_utc(row.created_at),
            updated_at=_utc(row.updated_at),
            submitted_at=_utc_or_none(row.submitted_at),
            terminal_at=_utc_or_none(row.terminal_at),
            rejection_code=RiskRejectionCode(row.rejection_code) if row.rejection_code else None,
            rejection_detail=row.rejection_detail,
            venue_rejection=row.venue_rejection,
            reconciliation_attempts=row.reconciliation_attempts,
            last_reconciled_at=_utc_or_none(row.last_reconciled_at),
            reconciliation_detail=row.reconciliation_detail,
            discrepancies=tuple(
                OrderDiscrepancy(
                    observed_at=_utc(d.observed_at),
                    detail=d.detail,
                    local_state=OrderState(d.local_state),
                    venue_state=OrderState(d.venue_state) if d.venue_state else None,
                )
                for d in discrepancies
            ),
            resolved_by_operator=row.resolved_by_operator,
            operator_reason=row.operator_reason,
        )


def _constraint_name(exc: BaseException) -> str:
    """Walk the wrapped-exception chain for the driver's constraint name."""
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < 8:
        name = getattr(current, "constraint_name", None)
        if name:
            return str(name)
        current = current.__cause__ or getattr(current, "orig", None)
        seen += 1
    return ""


def _utc(value: datetime) -> datetime:
    """PostgreSQL returns ``timestamptz`` already aware; this states the contract."""
    return value


def _utc_or_none(value: datetime | None) -> datetime | None:
    return value
