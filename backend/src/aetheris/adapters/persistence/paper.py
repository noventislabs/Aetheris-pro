"""Durable paper account state.

Paper state was the build's oldest disclosed limitation: balances, positions,
the order log, trade history and the daily session lived in process memory and
were gone on restart. Every account response said so. A database is configured
now, so that is a choice rather than a constraint.

## Why this is a store and not a ``PaperRepository``

``PaperRepository`` is synchronous, and the engine calls ``load()`` and
``save()`` in eighteen places across submit, tick, close, reset and the
emergency stop. Making it async would turn a working, heavily tested trading
engine inside out to gain nothing the engine itself needs -- it does not care
where its state came from.

So the engine keeps its in-memory repository and this sits beside it, at the
service layer, inside the write lock that already serialises every mutation:

* on startup the snapshot is read once and seeded into the repository;
* after each mutation completes, the new state is written back.

The lock is what makes that safe. It already covers fetch-then-mutate for
every path, so a write-back inside it cannot interleave with another mutation
and cannot store a half-applied state.

## A failed write degrades, it does not lose the trade

If the database is unreachable when the write-back runs, the mutation has
already happened and is correct in memory. Raising would tell the caller their
order failed when it did not. The store records the failure and reports
``IN_MEMORY`` durability instead, so the API stops claiming state will
survive a restart -- which is the honest half of the guarantee, and the half
that matters.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from aetheris.adapters.persistence.engine import DatabaseUnavailableError, session_scope
from aetheris.adapters.persistence.models import PaperSnapshotRow
from aetheris.core.logging import get_logger
from aetheris.domain.paper import Durability
from aetheris.engines.paper.snapshot import (
    SCHEMA_VERSION,
    SnapshotSchemaMismatch,
    from_snapshot,
    to_snapshot,
)
from aetheris.engines.paper.state import PaperState

__all__ = ["PostgresPaperSnapshotStore"]

_log = get_logger(__name__)


class PostgresPaperSnapshotStore:
    """One paper account's state, as a single row replaced in place.

    Scoped to one owner and one account taken in the constructor rather than
    per call, which is the same rule the order repository follows: there is no
    method to call that omits the owner, so a call cannot accidentally reach
    another tenant's row.
    """

    def __init__(
        self,
        factory: async_sessionmaker[Any],
        *,
        owner_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> None:
        self._factory = factory
        self._owner_id = owner_id
        self._account_id = account_id
        self._healthy = True
        self._last_error: str | None = None

    @property
    def durability(self) -> Durability:
        """``DURABLE`` only while writes are actually landing.

        A store that has failed its last write is not durable, whatever its
        configuration says, and continuing to advertise DURABLE would be the
        one lie this module exists to avoid.
        """
        return Durability.DURABLE if self._healthy else Durability.IN_MEMORY

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def load(self) -> PaperState | None:
        """Read the stored state, or ``None`` if there is none to read.

        ``None`` means "start fresh" and is the normal first-run answer. A
        snapshot written under a different schema is **also** refused here,
        and refusing is deliberate: an account whose balance restored while
        its open positions silently vanished is far worse than one that
        plainly started over.
        """
        try:
            async with session_scope(self._factory, str(self._owner_id)) as session:
                row = (
                    await session.execute(
                        select(PaperSnapshotRow).where(
                            PaperSnapshotRow.owner_id == self._owner_id,
                            PaperSnapshotRow.account_id == self._account_id,
                        )
                    )
                ).scalar_one_or_none()
        except (DatabaseUnavailableError, SQLAlchemyError) as exc:
            self._degrade(f"snapshot could not be read: {exc}")
            return None

        if row is None:
            return None

        try:
            state = from_snapshot(dict(row.state))
        except (SnapshotSchemaMismatch, ValueError, KeyError, TypeError) as exc:
            # Not a degrade: the database is fine, the document is not. The
            # account starts fresh and the reason is logged rather than
            # swallowed, because a silently reset balance is the worst
            # possible outcome here.
            _log.warning(
                "paper_snapshot_unreadable",
                detail=str(exc),
                account_id=str(self._account_id),
                expected_schema=SCHEMA_VERSION,
                stored_schema=row.schema_version,
            )
            return None

        _log.info(
            "paper_snapshot_restored",
            account_id=str(self._account_id),
            positions=len(state.positions),
            orders=len(state.order_log),
            trades=len(state.trades),
        )
        return state

    async def save(self, state: PaperState) -> bool:
        """Replace the stored snapshot. Returns whether the write landed.

        Never raises. The caller has already mutated its in-memory state
        correctly; telling it the order failed because a write did would be
        false, and rolling the mutation back would discard a trade the engine
        legitimately made. The failure surfaces as reduced durability instead.
        """
        try:
            document = to_snapshot(state)
        except (ValueError, TypeError) as exc:  # pragma: no cover - serialiser is total
            self._degrade(f"state could not be serialised: {exc}")
            return False

        try:
            async with session_scope(self._factory, str(self._owner_id)) as session:
                row = (
                    await session.execute(
                        select(PaperSnapshotRow).where(
                            PaperSnapshotRow.owner_id == self._owner_id,
                            PaperSnapshotRow.account_id == self._account_id,
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    session.add(
                        PaperSnapshotRow(
                            owner_id=self._owner_id,
                            account_id=self._account_id,
                            schema_version=SCHEMA_VERSION,
                            state=document,
                        )
                    )
                else:
                    row.schema_version = SCHEMA_VERSION
                    row.state = document
        except (DatabaseUnavailableError, SQLAlchemyError) as exc:
            self._degrade(f"snapshot could not be written: {exc}")
            return False

        if not self._healthy:
            # Recovered. Say so, because the API has been reporting IN_MEMORY
            # and is about to start reporting DURABLE again.
            _log.info("paper_snapshot_recovered", account_id=str(self._account_id))
        self._healthy = True
        self._last_error = None
        return True

    def _degrade(self, detail: str) -> None:
        if self._healthy:
            _log.warning(
                "paper_snapshot_degraded",
                detail=detail,
                account_id=str(self._account_id),
                durability=Durability.IN_MEMORY.value,
            )
        self._healthy = False
        self._last_error = detail
