"""Market-data orchestration.

Sits between the HTTP layer and the exchange adapter so the API depends on a
port rather than on Binance. Adding a second venue later means constructing a
different adapter here; no endpoint changes.

Search lives at this layer rather than in the adapter because it is a product
decision (how results are ranked) rather than a venue detail.
"""

from __future__ import annotations

from aetheris.adapters.exchange.errors import SymbolNotFoundError
from aetheris.adapters.exchange.ports import MarketDataPort
from aetheris.core.freshness import Observation
from aetheris.domain.enums import Timeframe
from aetheris.domain.market import (
    CandleSeries,
    ExchangeInfo,
    MarketDataStatus,
    Symbol,
    Ticker,
)


class MarketDataService:
    """Read-only market-data facade."""

    def __init__(self, exchange: MarketDataPort) -> None:
        self._exchange = exchange

    @property
    def exchange_name(self) -> str:
        return self._exchange.name

    async def aclose(self) -> None:
        await self._exchange.aclose()

    async def get_exchange_info(self) -> ExchangeInfo:
        return await self._exchange.get_exchange_info()

    async def get_symbols(self, *, eligible_only: bool = True) -> tuple[Symbol, ...]:
        return await self._exchange.get_symbols(eligible_only=eligible_only)

    async def get_symbol(self, symbol: str) -> Symbol:
        wanted = symbol.upper()
        for candidate in await self._exchange.get_symbols(eligible_only=False):
            if candidate.symbol == wanted:
                return candidate
        raise SymbolNotFoundError(
            f"Symbol {wanted} is not listed on {self._exchange.name}",
            details={"symbol": wanted},
        )

    async def search_symbols(
        self, query: str, *, limit: int, eligible_only: bool = True
    ) -> tuple[Symbol, ...]:
        """Rank matches against the dynamically discovered universe.

        Ranking is exact symbol, then exact base asset, then prefix, then
        substring. That ordering means "BTC" surfaces BTCUSDT above
        1000BTTCUSDT, while a query like "0G" still finds 0GUSDT -- no symbol
        is special-cased anywhere.
        """
        needle = query.strip().upper()
        if not needle:
            return ()

        universe = await self._exchange.get_symbols(eligible_only=eligible_only)
        scored: list[tuple[int, str, Symbol]] = []
        for candidate in universe:
            if candidate.symbol == needle:
                rank = 0
            elif candidate.base_asset == needle:
                rank = 1
            elif candidate.symbol.startswith(needle) or candidate.base_asset.startswith(needle):
                rank = 2
            elif needle in candidate.symbol:
                rank = 3
            else:
                continue
            scored.append((rank, candidate.symbol, candidate))

        scored.sort(key=lambda item: (item[0], item[1]))
        return tuple(symbol for _, _, symbol in scored[:limit])

    async def get_ticker(self, symbol: str) -> Observation[Ticker]:
        return await self._exchange.get_ticker(symbol)

    async def get_klines(
        self, symbol: str, timeframe: Timeframe, *, limit: int
    ) -> Observation[CandleSeries]:
        return await self._exchange.get_klines(symbol, timeframe, limit=limit)

    async def get_status(self) -> MarketDataStatus:
        return await self._exchange.get_market_status()
