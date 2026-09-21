"""Order record storage.

The interface exists now, ahead of any database, for the same reason
``PaperRepository`` did in phase 6: the durable implementation should be a new
class rather than a rewrite of the engine.

**The in-memory implementation is honest about what it is, and here that is a
sharper point than it was for paper.** Paper state that vanishes on restart
loses a simulation. Order state that vanishes on restart loses the record of
something that may exist at a venue -- which is precisely the record recovery
depends on. So this implementation reports ``durable = False``, and phase 8a
deliberately does **not** claim crash recovery: the protocol is built and
tested here, and only becomes a real guarantee when phase 8b puts it on
PostgreSQL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from aetheris.domain.order import OrderRecord

__all__ = ["InMemoryOrderRepository", "OrderRepository"]


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
    def add(self, record: OrderRecord) -> OrderRecord:
        """Store a new record. Must reject a duplicate client order id."""

    @abstractmethod
    def update(self, record: OrderRecord) -> OrderRecord:
        """Replace an existing record."""

    @abstractmethod
    def get(self, order_id: str) -> OrderRecord | None: ...

    @abstractmethod
    def get_by_client_order_id(self, client_order_id: str) -> OrderRecord | None:
        """The lookup recovery uses. This is why identity must be derivable."""

    @abstractmethod
    def all_records(self) -> tuple[OrderRecord, ...]: ...

    @abstractmethod
    def open_records(self) -> tuple[OrderRecord, ...]:
        """Every non-terminal record -- the set a recovery pass must ask about."""

    @abstractmethod
    def clear(self) -> None:
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

    @property
    def durable(self) -> bool:
        return False

    def add(self, record: OrderRecord) -> OrderRecord:
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

    def update(self, record: OrderRecord) -> OrderRecord:
        if record.order_id not in self._by_order_id:
            raise KeyError(f"No order {record.order_id} to update")
        self._by_order_id[record.order_id] = record
        return record

    def get(self, order_id: str) -> OrderRecord | None:
        return self._by_order_id.get(order_id)

    def get_by_client_order_id(self, client_order_id: str) -> OrderRecord | None:
        order_id = self._by_client_id.get(client_order_id)
        return self._by_order_id.get(order_id) if order_id else None

    def all_records(self) -> tuple[OrderRecord, ...]:
        return tuple(self._by_order_id.values())

    def open_records(self) -> tuple[OrderRecord, ...]:
        return tuple(r for r in self._by_order_id.values() if not r.is_terminal)

    def clear(self) -> None:
        self._by_order_id.clear()
        self._by_client_id.clear()
