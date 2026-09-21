"""Order record storage.

The interface exists now, ahead of any database, for the same reason
``PaperRepository`` did in phase 6: the durable implementation should be a new
class rather than a rewrite of the engine.

**The in-memory implementation is honest about what it is, and here that is a
sharper point than it was for paper.** Paper state that vanishes on restart
loses a simulation. Order state that vanishes on restart loses the record of
something that may exist at a venue -- which is precisely the record recovery
depends on. So this implementation reports ``durable = False``: it remains the
development store, and nothing that reports ``False`` may claim crash recovery.
The durable implementation lives in ``adapters.persistence``.

**The port is async.** Every method awaits, because the durable implementation
is a network round-trip and a synchronous call would block the event loop for
its latency on every order. The in-memory store gains nothing from this and
pays nothing for it; the shape exists so that swapping the implementation is a
composition change rather than a rewrite of every caller.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from aetheris.domain.order import OrderRecord

__all__ = ["DuplicateClientOrderIdError", "InMemoryOrderRepository", "OrderRepository"]


class OrderRepository(ABC):
    """Storage for order records.

    Deliberately narrow. Everything the lifecycle needs is here and nothing
    else, so a PostgreSQL implementation in phase 8b has a small, obvious
    surface to satisfy -- including the two properties that matter for
    correctness there: a unique constraint on the client order id, and
    single-flight reconciliation of one order.
    """

    @property
    @abstractmethod
    def durable(self) -> bool:
        """Whether records survive a process restart.

        ``False`` means crash recovery is not available, whatever the
        reconciliation protocol is capable of.
        """

    @abstractmethod
    async def add(self, record: OrderRecord) -> OrderRecord:
        """Store a new record. Must reject a duplicate client order id."""

    @abstractmethod
    async def update(self, record: OrderRecord) -> OrderRecord:
        """Replace an existing record."""

    @abstractmethod
    async def get(self, order_id: str) -> OrderRecord | None: ...

    @abstractmethod
    def locked(self, order_id: str) -> AbstractAsyncContextManager[OrderRecord | None]:
        """Hold one order for the duration of a block, excluding other workers.

        This is what makes reconciliation single-flight. Without it two passes
        can both read ``UNKNOWN``, both ask the venue, and both apply a
        conclusion -- and if they disagree, the last writer wins silently.

        One order, not a global lock: a global lock would serialise every order
        behind the slowest reconciliation. The durable implementation is
        ``SELECT ... FOR UPDATE`` inside one transaction, which serialises
        across processes; see ``InMemoryOrderRepository.locked`` for what an
        in-process store can and cannot promise.
        """

    @abstractmethod
    async def get_by_client_order_id(self, client_order_id: str) -> OrderRecord | None:
        """The lookup recovery uses. This is why identity must be derivable."""

    @abstractmethod
    async def all_records(self) -> tuple[OrderRecord, ...]: ...

    @abstractmethod
    async def open_records(self) -> tuple[OrderRecord, ...]:
        """Every non-terminal record -- the set a recovery pass must ask about."""

    @abstractmethod
    async def clear(self) -> None:
        """Discard everything. Development and tests only."""


class DuplicateClientOrderIdError(ValueError):
    """Two orders cannot share an identity.

    In phase 8b this becomes a database unique constraint, so the guarantee
    holds even when two processes race. Here it is enforced in the dictionary,
    which is the same guarantee with a smaller blast radius.
    """


class InMemoryOrderRepository(OrderRepository):
    """Development storage. Not restart-safe, and says so."""

    def __init__(self) -> None:
        self._by_order_id: dict[str, OrderRecord] = {}
        self._by_client_id: dict[str, str] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def durable(self) -> bool:
        return False

    async def add(self, record: OrderRecord) -> OrderRecord:
        if record.client_order_id in self._by_client_id:
            raise DuplicateClientOrderIdError(
                f"An order with client id {record.client_order_id} already exists. "
                "Identity is derived from intent, so a duplicate means the same intent "
                "was submitted twice -- which is exactly what the identity exists to "
                "prevent."
            )
        self._by_order_id[record.order_id] = record
        self._by_client_id[record.client_order_id] = record.order_id
        return record

    async def update(self, record: OrderRecord) -> OrderRecord:
        if record.order_id not in self._by_order_id:
            raise KeyError(f"No order {record.order_id} to update")
        self._by_order_id[record.order_id] = record
        return record

    async def get(self, order_id: str) -> OrderRecord | None:
        return self._by_order_id.get(order_id)

    async def get_by_client_order_id(self, client_order_id: str) -> OrderRecord | None:
        order_id = self._by_client_id.get(client_order_id)
        return self._by_order_id.get(order_id) if order_id else None

    async def all_records(self) -> tuple[OrderRecord, ...]:
        return tuple(self._by_order_id.values())

    async def open_records(self) -> tuple[OrderRecord, ...]:
        return tuple(r for r in self._by_order_id.values() if not r.is_terminal)

    @asynccontextmanager
    async def locked(self, order_id: str) -> AsyncIterator[OrderRecord | None]:
        """Serialise within this process only.

        An ``asyncio.Lock`` excludes two coroutines in one interpreter. It does
        not exclude a second process, which is exactly the case phase 8b's
        recovery pass creates. That gap is not closed by trying harder here --
        it is closed by a row lock in a database, and this store reports
        ``durable = False`` for the same reason.
        """
        async with self._locks[order_id]:
            yield self._by_order_id.get(order_id)

    async def clear(self) -> None:
        self._by_order_id.clear()
        self._by_client_id.clear()
        self._locks.clear()
