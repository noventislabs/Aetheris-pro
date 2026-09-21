"""Owners and accounts.

**Phase 1 has no authentication yet, and this file does not pretend otherwise.**
It resolves a single bootstrap owner, server-side, from a constant. That keeps
phase 6's security property exactly as it was: no request names or selects an
account, so a caller cannot ask for someone else's -- there is no identifier to
ask with. What changes is that the isolation *mechanism* now exists and is
enforced by the database, so the authentication work that follows has somewhere
to plug in rather than a set of queries to audit.

The owner identifier is derived deterministically rather than looked up. That
is not a shortcut, it is forced by the isolation design: the ``users`` policy
scopes rows to ``id = current_setting('aetheris.owner_id')``, so the runtime
role cannot find a user by email -- it can only confirm the one it already
names. An authentication path needs a lookup the runtime role is not allowed to
make, which is a ``SECURITY DEFINER`` function or a second narrowly-scoped
policy. Either is a deliberate hole in the wall and belongs in the change that
introduces login, not in this one.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aetheris.adapters.persistence.engine import DatabaseUnavailableError, session_scope
from aetheris.adapters.persistence.models import AccountRow, UserRow
from aetheris.domain.enums import TradingMode

__all__ = ["BOOTSTRAP_OWNER_EMAIL", "BOOTSTRAP_OWNER_ID", "PostgresAccountRepository"]

#: The single local owner, until authentication issues real ones. Derived with
#: uuid5 so it is the same value on every boot and on every machine, which is
#: what makes the account survive a restart without a lookup the runtime role
#: is not permitted to perform.
BOOTSTRAP_OWNER_ID: Final = uuid.uuid5(uuid.NAMESPACE_URL, "https://aetheris.local/owner/bootstrap")
BOOTSTRAP_OWNER_EMAIL: Final = "bootstrap@aetheris.local"


class PostgresAccountRepository:
    """Creates and finds the rows everything else hangs off."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def ensure_owner(
        self,
        *,
        owner_id: uuid.UUID = BOOTSTRAP_OWNER_ID,
        email: str = BOOTSTRAP_OWNER_EMAIL,
        display_name: str = "Local operator",
    ) -> uuid.UUID:
        """Create the owner row if it is not there. Safe to call on every boot."""
        try:
            async with session_scope(self._factory, str(owner_id)) as session:
                existing = (
                    await session.execute(select(UserRow.id).where(UserRow.id == owner_id))
                ).scalar_one_or_none()
                if existing is None:
                    session.add(UserRow(id=owner_id, email=email, display_name=display_name))
                    await session.flush()
            return owner_id
        except SQLAlchemyError as exc:
            raise DatabaseUnavailableError(
                f"could not resolve the owner record: {type(exc).__name__}"
            ) from None

    async def ensure_account(
        self,
        *,
        owner_id: uuid.UUID,
        mode: TradingMode,
        starting_balance: Decimal,
    ) -> uuid.UUID:
        """Return this owner's account for a mode, creating it once.

        ``UNIQUE (owner_id, mode)`` makes "exactly one account per mode" a
        database fact rather than a convention that held only because the
        in-memory store had no way to make a second one.
        """
        try:
            async with session_scope(self._factory, str(owner_id)) as session:
                existing = (
                    await session.execute(
                        select(AccountRow.id).where(
                            AccountRow.owner_id == owner_id,
                            AccountRow.mode == mode.value,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return existing
                row = AccountRow(
                    owner_id=owner_id,
                    mode=mode.value,
                    starting_balance=starting_balance,
                )
                session.add(row)
                await session.flush()
                return row.id
        except SQLAlchemyError as exc:
            raise DatabaseUnavailableError(
                f"could not resolve the account record: {type(exc).__name__}"
            ) from None
