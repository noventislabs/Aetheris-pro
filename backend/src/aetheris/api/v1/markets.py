"""Read-only market-data endpoints.

Every route here is a GET. Nothing in this module can place, amend or cancel an
order, and the service it depends on exposes no method that could.

Responses carry normalized Aetheris models, not venue payloads, and anything
sourced externally arrives inside an observation envelope so the caller always
sees where a number came from and how old it is.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, Field

from aetheris.api.deps import MarketDataDep
from aetheris.core.freshness import DataStatus, Observation
from aetheris.domain.enums import Timeframe
from aetheris.domain.market import (
    CandleSeries,
    ExchangeInfo,
    MarketDataStatus,
    Symbol,
    Ticker,
)

router = APIRouter(prefix="/markets", tags=["markets"])

#: Venue tickers are uppercase alphanumerics. Constraining the path segment
#: keeps unvalidated input out of an upstream query string and gives a 422
#: before any network call is made.
SymbolPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Instrument symbol, e.g. BTCUSDT",
        examples=["BTCUSDT"],
    ),
]


class ObservationEnvelope[T](BaseModel):
    """Serializable form of :class:`~aetheris.core.freshness.Observation`.

    ``value`` is populated only when ``status`` is OK -- the domain invariant
    is preserved across the wire, so a client that ignores ``status`` receives
    ``null`` rather than a stale number it might render as live.
    """

    status: DataStatus
    source: str
    event_ts: datetime | None = None
    received_ts: datetime
    age_seconds: float | None = None
    detail: str | None = None
    value: T | None = None

    @classmethod
    def of(cls, observation: Observation[T]) -> ObservationEnvelope[T]:
        return cls(
            status=observation.status,
            source=observation.source,
            event_ts=observation.event_ts,
            received_ts=observation.received_ts,
            age_seconds=observation.age_seconds,
            detail=observation.detail,
            value=observation.value,
        )


class SymbolListResponse(BaseModel):
    count: int
    eligible_only: bool
    symbols: list[Symbol]


class ExchangeInfoResponse(BaseModel):
    exchange: str
    server_time: datetime | None
    symbol_count: int = Field(description="Total instruments the venue listed")
    eligible_count: int = Field(description="Instruments passing the eligibility filter")
    symbols: list[Symbol]


@router.get("/status", response_model=MarketDataStatus, summary="Market-data connectivity")
async def market_status(service: MarketDataDep) -> MarketDataStatus:
    """Observed connectivity. Never reports CONNECTED without a real response.

    Deliberately does not trigger an upstream call: a monitoring poll must not
    become rate-limit pressure.
    """
    return await service.get_status()


@router.get(
    "/exchange-info",
    response_model=ExchangeInfoResponse,
    summary="Venue metadata and instrument list",
)
async def exchange_info(
    service: MarketDataDep,
    include_symbols: Annotated[
        bool, Query(description="Include the full symbol array in the response")
    ] = False,
) -> ExchangeInfoResponse:
    """Normalized exchange metadata.

    The symbol array is omitted by default: it is several hundred entries, and
    most callers want the counts or the filtered list from ``/symbols``.
    """
    info: ExchangeInfo = await service.get_exchange_info()
    eligible = await service.get_symbols(eligible_only=True)
    return ExchangeInfoResponse(
        exchange=info.exchange,
        server_time=info.server_time,
        symbol_count=info.symbol_count,
        eligible_count=len(eligible),
        symbols=list(info.symbols) if include_symbols else [],
    )


@router.get(
    "/symbols",
    response_model=SymbolListResponse,
    summary="Discovered symbols, with optional search",
)
async def list_symbols(
    service: MarketDataDep,
    search: Annotated[
        str | None,
        Query(max_length=32, description="Match against symbol or base asset, e.g. BTC or 0G"),
    ] = None,
    eligible_only: Annotated[
        bool, Query(description="Restrict to USDT-M perpetuals that are TRADING")
    ] = True,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> SymbolListResponse:
    """Dynamically discovered universe -- no symbol list is hardcoded anywhere."""
    if search:
        symbols = await service.search_symbols(search, limit=limit, eligible_only=eligible_only)
    else:
        symbols = (await service.get_symbols(eligible_only=eligible_only))[:limit]
    return SymbolListResponse(
        count=len(symbols), eligible_only=eligible_only, symbols=list(symbols)
    )


@router.get(
    "/symbols/{symbol}",
    response_model=Symbol,
    summary="One symbol's metadata and venue filters",
)
async def get_symbol(service: MarketDataDep, symbol: SymbolPath) -> Symbol:
    return await service.get_symbol(symbol)


@router.get(
    "/{symbol}/ticker",
    response_model=ObservationEnvelope[Ticker],
    summary="Latest ticker with provenance",
)
async def get_ticker(service: MarketDataDep, symbol: SymbolPath) -> ObservationEnvelope[Ticker]:
    return ObservationEnvelope[Ticker].of(await service.get_ticker(symbol))


@router.get(
    "/{symbol}/klines",
    response_model=ObservationEnvelope[CandleSeries],
    summary="Historical candles with provenance",
)
async def get_klines(
    service: MarketDataDep,
    symbol: SymbolPath,
    interval: Annotated[
        Timeframe, Query(description="One of 1m, 5m, 15m, 1h, 4h, 1d")
    ] = Timeframe.H1,
    limit: Annotated[
        int, Query(ge=1, le=1500, description="Bounded to the venue's own ceiling")
    ] = 200,
) -> ObservationEnvelope[CandleSeries]:
    """Candles for one symbol and timeframe.

    ``limit`` is bounded at both ends so a request cannot ask for an
    unbounded history and exhaust memory on the server or the venue's patience.
    """
    return ObservationEnvelope[CandleSeries].of(
        await service.get_klines(symbol, interval, limit=limit)
    )
