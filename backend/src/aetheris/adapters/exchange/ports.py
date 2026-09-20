"""Exchange ports -- the contracts the rest of the system codes against.

Two contracts, split on purpose:

:class:`MarketDataPort`
    Read-only. Implemented by the Binance adapter in this phase, and by any
    future venue without the market-data layer above it changing.

:class:`TradingPort`
    Execution. **Declared but implemented by nothing.** It exists so the
    eventual shape is visible and so a test can assert that no adapter has
    grown order-placing methods ahead of the phase that reviews them. Because
    it is a ``Protocol`` with no implementation, there is no runtime path to an
    order in this build -- the methods are types, not code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from aetheris.core.freshness import Observation
from aetheris.domain.enums import Timeframe
from aetheris.domain.market import (
    CandleSeries,
    ExchangeInfo,
    MarketDataStatus,
    Symbol,
    Ticker,
)


class MarketDataPort(ABC):
    """Read-only market data from a venue.

    Implementations must never fabricate: each method either returns a real
    observation or raises an
    :class:`~aetheris.adapters.exchange.errors.ExchangeError`.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable venue identifier, e.g. ``binance-futures-usdm``."""

    @property
    @abstractmethod
    def source(self) -> str:
        """Provenance label attached to observations, e.g. ``...:rest``."""

    @abstractmethod
    async def get_exchange_info(self) -> ExchangeInfo:
        """Venue metadata and the full instrument list."""

    @abstractmethod
    async def get_symbols(self, *, eligible_only: bool = True) -> tuple[Symbol, ...]:
        """Discovered instruments, optionally filtered to the eligible universe."""

    @abstractmethod
    async def get_ticker(self, symbol: str) -> Observation[Ticker]:
        """Latest ticker, wrapped with provenance and freshness."""

    @abstractmethod
    async def get_klines(
        self, symbol: str, timeframe: Timeframe, *, limit: int
    ) -> Observation[CandleSeries]:
        """Historical candles, wrapped with provenance and freshness."""

    @abstractmethod
    async def get_market_status(self) -> MarketDataStatus:
        """Observed connection state. Never claims CONNECTED without evidence."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release network resources."""


@runtime_checkable
class TradingPort(Protocol):
    """Execution contract -- NOT IMPLEMENTED, and not implementable by accident.

    Nothing in this build satisfies this protocol. It is here to pin the future
    shape and to give the architecture test something concrete to assert
    against: if an adapter ever starts satisfying it outside the execution
    phases, the suite fails.
    """

    async def get_balance(self) -> object: ...

    async def get_positions(self) -> object: ...

    async def create_order(self, *args: object, **kwargs: object) -> object: ...

    async def cancel_order(self, *args: object, **kwargs: object) -> object: ...

    async def get_order(self, *args: object, **kwargs: object) -> object: ...
