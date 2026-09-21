"""Paper state storage.

The repository interface exists now, ahead of any database, so the durable
implementation is a new class rather than a rewrite of the engine. The engine
never touches a dict directly -- it asks the repository.

``InMemoryPaperRepository`` is honest about what it is: state lives in this
process and is gone when it stops. It reports ``Durability.IN_MEMORY`` and the
API repeats that on every response, because a paper balance that silently
resets is worse than one the user knows resets.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from aetheris.domain.paper import (
    Durability,
    ReconciliationReport,
    ReconciliationStatus,
)
from aetheris.engines.paper.state import PaperState

DEFAULT_ACCOUNT_ID = "paper-default"


class PaperRepository(ABC):
    """Storage for paper account state."""

    @property
    @abstractmethod
    def durability(self) -> Durability:
        """How long this store's state survives."""

    @abstractmethod
    def load(self) -> PaperState:
        """Return the account state, creating it on first use."""

    @abstractmethod
    def save(self, state: PaperState) -> None:
        """Persist the state. A no-op for an in-memory store."""

    @abstractmethod
    def reset(self, *, starting_balance: Decimal, now: datetime) -> PaperState:
        """Discard everything and start a fresh account."""

    @abstractmethod
    def reconcile(self, now: datetime) -> ReconciliationReport:
        """Check local state against an external authority, if there is one."""


class InMemoryPaperRepository(PaperRepository):
    """Development storage. Not restart-safe, and says so.

    There is exactly **one** account, created server-side. No request can name
    or select an account, which is what keeps this free of an IDOR while
    authentication does not exist yet: a caller cannot ask for someone else's
    account because there is no identifier to ask with. Multi-tenant isolation
    needs the authentication and database work in phase 1.
    """

    def __init__(self, *, starting_balance: Decimal, now: datetime) -> None:
        self._starting_balance = starting_balance
        self._state = self._fresh(starting_balance, now)

    @property
    def durability(self) -> Durability:
        return Durability.IN_MEMORY

    @staticmethod
    def _fresh(starting_balance: Decimal, now: datetime) -> PaperState:
        return PaperState(
            account_id=DEFAULT_ACCOUNT_ID,
            created_at=now,
            updated_at=now,
            starting_balance=starting_balance,
            balance=starting_balance,
        )

    def load(self) -> PaperState:
        return self._state

    def save(self, state: PaperState) -> None:
        # The engine mutates the same object this store holds, so there is
        # nothing to write. The call stays in the flow so the durable
        # implementation has the hook it needs without the engine changing.
        self._state = state

    def reset(self, *, starting_balance: Decimal, now: datetime) -> PaperState:
        self._starting_balance = starting_balance
        self._state = self._fresh(starting_balance, now)
        return self._state

    def reconcile(self, now: datetime) -> ReconciliationReport:
        """Nothing external to reconcile against.

        Reported as NOT_APPLICABLE rather than CONSISTENT: claiming consistency
        would imply a comparison that never happened.
        """
        return ReconciliationReport(
            status=ReconciliationStatus.NOT_APPLICABLE,
            detail=(
                "Paper state is held in this process and has no external authority to "
                "reconcile against. Real reconciliation begins when orders reach a "
                "venue and state is persisted."
            ),
            checked_at=now,
            positions_checked=len(self._state.positions),
            orders_checked=len(self._state.order_log),
        )
