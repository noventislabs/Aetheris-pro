"""The tables, and the reasoning behind the column types.

Two choices are not negotiable and are stated here rather than in a migration,
because a migration is read once and a model is read constantly.

**Money is ``NUMERIC(24,8)``.** ``core.money`` rejects ``float`` at runtime. A
system that refuses ``Decimal(0.1)`` in Python and stores it as
``double precision`` in PostgreSQL has accomplished nothing: the rounding just
moves to the write. Twenty-four digits with eight after the point covers every
quantity and notional a USDT-margined venue quotes, with room left over.

**Time is ``timestamptz``.** ``timestamp without time zone`` discards the
offset, and order timing across a restart is UTC or it is wrong. "Wrong by an
hour" during a DST change is the kind of bug that appears twice a year and is
diagnosed neither time.

``owner_id`` is carried on every table rather than reached through a join.
A row-level security policy runs per row, and a policy that reaches
``accounts`` through a subquery pays that join on every read and risks
recursion once ``accounts`` has a policy of its own. A denormalised owner makes
each policy a column comparison, which is the cheapest correct thing.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "MONEY",
    "AccountRow",
    "Base",
    "OrderDiscrepancyRow",
    "OrderFillRow",
    "OrderRow",
    "UserRow",
]

#: One definition, used everywhere money or quantity is stored.
MONEY = Numeric(24, 8)

_UUID = PgUUID(as_uuid=True)
_TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(_UUID, primary_key=True, server_default=text("gen_random_uuid()"))


class UserRow(Base):
    """The owner of everything else.

    Phase 1 creates the table and the foreign keys that depend on it.
    Authentication -- issuing a session that resolves to exactly one of these
    -- is the other half of the same phase, and until it lands the application
    resolves a single bootstrap owner server-side. No request names a user.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[dt.datetime] = mapped_column(
        _TS, nullable=False, server_default=text("now()")
    )


class AccountRow(Base):
    """One account per user per mode.

    The unique constraint makes phase 6's "there is exactly one paper account"
    a database fact rather than a convention held up by the in-memory store
    having no way to make a second one.
    """

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("owner_id", "mode", name="uq_accounts_owner_mode"),
        CheckConstraint("mode IN ('PAPER','TESTNET','LIVE')", name="ck_accounts_mode"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    starting_balance: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    #: Who we are at the venue, so records cannot be silently re-attributed to
    #: a different venue account after a key swap.
    venue_account_id: Mapped[str | None] = mapped_column(String(64))
    #: A **name** that resolves to an environment variable, never a secret. A
    #: CHECK constraint confines it to a shape no key could occupy.
    credential_ref: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[dt.datetime] = mapped_column(
        _TS, nullable=False, server_default=text("now()")
    )


class OrderRow(Base):
    """The record recovery depends on.

    ``submitted_at`` is written in the same transaction as the ``SUBMITTED``
    state and committed **before** the network call. A crash in the window that
    follows leaves exactly the signature a recovery pass reads: ``submitted_at``
    present, ``venue_order_id`` null. Committing the stamp separately from the
    state would make that window ambiguous instead of observable.
    """

    __tablename__ = "orders"
    __table_args__ = (
        # Identity is derived from intent, so a duplicate means the same intent
        # was submitted twice. Enforced here rather than in the application
        # because a recovery pass and a live request can check-then-insert
        # concurrently, and a dictionary cannot serialise across processes.
        UniqueConstraint("account_id", "client_order_id", name="uq_orders_account_client_id"),
        # One venue order belongs to at most one local record. Indexed before,
        # constrained now: two local rows claiming one venue order is exactly
        # the confusion reconciliation is supposed to resolve, not create.
        UniqueConstraint("account_id", "venue_order_id", name="uq_orders_account_venue_order_id"),
        Index("ix_orders_account_state", "account_id", "state"),
        # For a future push stream keyed by the venue's identifier, not ours.
        Index("ix_orders_venue_order_id", "venue_order_id"),
        Index("ix_orders_owner", "owner_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )

    #: The application's identifier for the order. Distinct from ``id`` because
    #: the engine mints it before any row exists.
    order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    venue_order_id: Mapped[str | None] = mapped_column(String(64))

    state: Mapped[str] = mapped_column(String(24), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    intent_key: Mapped[str] = mapped_column(Text, nullable=False)
    reduce_only: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    price: Mapped[Decimal | None] = mapped_column(MONEY)
    filled_quantity: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    average_fill_price: Mapped[Decimal | None] = mapped_column(MONEY)

    created_at: Mapped[dt.datetime] = mapped_column(_TS, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(_TS, nullable=False)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(_TS)
    terminal_at: Mapped[dt.datetime | None] = mapped_column(_TS)

    rejection_code: Mapped[str | None] = mapped_column(String(64))
    rejection_detail: Mapped[str | None] = mapped_column(Text)
    venue_rejection: Mapped[str | None] = mapped_column(Text)

    #: Exactly what the venue said, before mapping. EXPIRED_IN_MATCH becomes
    #: EXPIRED in the domain by decision; this is where that distinction
    #: survives for whoever reads the history later.
    venue_status_raw: Mapped[str | None] = mapped_column(String(32))
    #: Distinguishes "asked, nothing had changed" from "never asked".
    last_polled_at: Mapped[dt.datetime | None] = mapped_column(_TS)
    #: The execution parameters the venue **confirmed** before the order was
    #: allowed to leave. Recorded so a fill can be audited against what was
    #: actually in force, not what was requested.
    venue_leverage: Mapped[Decimal | None] = mapped_column(MONEY)
    venue_margin_mode: Mapped[str | None] = mapped_column(String(16))

    reconciliation_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_reconciled_at: Mapped[dt.datetime | None] = mapped_column(_TS)
    reconciliation_detail: Mapped[str | None] = mapped_column(Text)

    #: Retry state has to be durable, or a retry storm survives a restart
    #: invisibly. Written by phase 8b; the columns exist now so 8b is a port
    #: rather than a migration plus a port.
    submission_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    next_retry_at: Mapped[dt.datetime | None] = mapped_column(_TS)

    #: Manual settlement, always attributed.
    resolved_by_operator: Mapped[str | None] = mapped_column(String(120))
    operator_reason: Mapped[str | None] = mapped_column(Text)


class OrderFillRow(Base):
    """One execution against one order.

    ``UNIQUE (order_id, venue_trade_id)`` stops a replayed venue message from
    double-counting a fill. The constraint is partial in effect rather than in
    definition: ``venue_trade_id`` is null until a venue supplies one, and
    PostgreSQL treats nulls as distinct, so locally-generated fills do not
    collide with each other.
    """

    __tablename__ = "order_fills"
    __table_args__ = (
        UniqueConstraint("order_id", "venue_trade_id", name="uq_order_fills_venue_trade"),
        UniqueConstraint("order_id", "fill_id", name="uq_order_fills_fill_id"),
        Index("ix_order_fills_order", "order_id"),
        # PostgreSQL does not index the referencing side of a foreign key, so
        # every cascade from `users` would scan this table. Free while it is
        # empty; not free later, which is why it is added now.
        Index("ix_order_fills_owner", "owner_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    fill_id: Mapped[str] = mapped_column(String(64), nullable=False)
    venue_trade_id: Mapped[str | None] = mapped_column(String(64))
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fee: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default=text("0"))
    filled_at: Mapped[dt.datetime] = mapped_column(_TS, nullable=False)


class OrderDiscrepancyRow(Base):
    """Evidence. Append-only: nothing updates or deletes a row here."""

    __tablename__ = "order_discrepancies"
    __table_args__ = (
        Index("ix_order_discrepancies_order", "order_id"),
        # Same reasoning as order_fills: the cascade from `users` needs it.
        Index("ix_order_discrepancies_owner", "owner_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    observed_at: Mapped[dt.datetime] = mapped_column(_TS, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    local_state: Mapped[str] = mapped_column(String(24), nullable=False)
    venue_state: Mapped[str | None] = mapped_column(String(24))


class PaperSnapshotRow(Base):
    """One paper account's whole state, as a single replaceable document.

    Deliberately not a set of related tables. Order records earn typed columns
    because they are evidence a venue might contradict and a recovery pass has
    to query; paper state is simulation state this process owns outright, is
    reset wholesale by ``POST /paper/reset``, and is never compared against an
    outside authority. A dozen joined tables would buy query shapes nobody
    needs and put a migration in front of every future engine field.

    ``schema_version`` is stored as a column rather than left inside the
    document so a mismatch is visible to a query, not only to the loader.

    Money inside ``state`` crosses as **strings**, never JSON numbers: a JSON
    number is a float to almost every reader, and a paper balance that drifts
    by a cent per restart is a bug found months later and never explained.
    """

    __tablename__ = "paper_state_snapshots"
    __table_args__ = (
        UniqueConstraint("owner_id", "account_id", name="uq_paper_snapshot_owner_account"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        _TS, nullable=False, server_default=text("now()")
    )
