"""Exchange ports -- the contracts the rest of the system codes against.

Two contracts, split on purpose:

:class:`MarketDataPort`
    Read-only. Implemented by the Binance adapter in this phase, and by any
    future venue without the market-data layer above it changing.

:class:`TradingPort`
    Execution. Implemented in phase 8b by the **testnet** adapter and by
    nothing else. The market-data adapter must never satisfy it, and the
    architecture test asserts that it does not: the read path and the write
    path stay different objects so that reaching one cannot reach the other.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Protocol, runtime_checkable

from aetheris.core.freshness import Observation
from aetheris.domain.enums import Timeframe, TradingMode
from aetheris.domain.market import (
    CandleSeries,
    ExchangeInfo,
    MarketDataStatus,
    Symbol,
    Ticker,
)
from aetheris.domain.order import OrderRecord, VenueOrderView
from aetheris.domain.venue import LeverageBracket, MarginMode, PositionMode, VenueAccount


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
    async def get_all_tickers(self) -> Observation[tuple[Ticker, ...]]:
        """Whole-market ticker snapshot in a single request.

        Exists so a market scan costs one upstream call rather than one per
        instrument; with several hundred eligible perpetuals the per-symbol
        approach is not a slower option, it is a rate-limit ban.
        """

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
    """Execution contract, in domain vocabulary only.

    It said ``*args: object, **kwargs: object`` for six phases, which was the
    right placeholder while nothing implemented it and the wrong contract the
    moment something did: those signatures accept every call, so the type
    checker could not have caught a wrong one.

    No Binance vocabulary crosses this line. The adapter translates; everything
    above it speaks ``OrderRecord``, ``VenueOrderView`` and the venue types in
    ``domain.venue``.

    ``venue_mode`` is a property rather than a constructor detail so a caller
    can assert what it is talking to. In phase 8b the only implementation
    reports ``TESTNET``, and the architecture test asserts nothing reports
    ``LIVE``.
    """

    @property
    def venue_mode(self) -> TradingMode:
        """Which mode this adapter executes in. Never inferred by a caller."""
        ...

    async def account_identity(self) -> VenueAccount:
        """Who we are at the venue, and whether it will let us trade."""
        ...

    async def position_mode(self) -> PositionMode:
        """One-way or hedge. This build refuses hedge rather than adapting."""
        ...

    async def leverage_bracket(self, symbol: str, *, notional: Decimal) -> LeverageBracket:
        """The ceiling for the tier this order's notional actually falls in."""
        ...

    async def set_leverage(self, symbol: str, leverage: Decimal) -> Decimal:
        """Set initial leverage and return what the venue says it applied.

        Returning the applied value rather than ``None`` is the whole point:
        the caller verifies it, because a set that silently did something else
        would put an order on the book at a leverage risk never approved.
        """
        ...

    async def margin_mode(self, symbol: str) -> MarginMode:
        """Read the symbol's current margin mode."""
        ...

    async def set_margin_mode(self, symbol: str, mode: MarginMode) -> None:
        """Request a margin mode. Raises if the venue refuses the change."""
        ...

    async def submit(self, record: OrderRecord) -> VenueOrderView:
        """Place the order the record describes. Never called without one."""
        ...

    async def query(self, *, symbol: str, client_order_id: str) -> VenueOrderView | None:
        """Ask about one order. ``None`` means the venue has no such order.

        ``None`` is not proof the order never existed -- only the caller knows
        whether it was ever sent, and only that makes absence meaningful.
        """
        ...

    async def cancel(self, *, symbol: str, client_order_id: str) -> VenueOrderView:
        """Request cancellation. Losing the race to a fill is not an error."""
        ...

    async def open_orders(self, *, symbol: str | None = None) -> tuple[VenueOrderView, ...]:
        """Every order the venue still considers open."""
        ...

    async def aclose(self) -> None:
        """Release network resources."""
        ...
