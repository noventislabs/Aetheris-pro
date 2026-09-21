"""Binance USDT-M Futures public market-data adapter.

Read-only by construction: it holds no credentials, sends no authenticated
headers, and implements :class:`MarketDataPort` only. There is no code path
from this class to an order.

Caching stores *domain objects*, never ``Observation`` envelopes. Freshness is
recomputed from the exchange timestamp on every serve, so a cached ticker can
never be handed back wearing a stale OK status -- which is the failure mode a
cache in front of market data exists to avoid.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx

from aetheris.adapters.exchange.binance import endpoints, parsing
from aetheris.adapters.exchange.cache import TTLCache
from aetheris.adapters.exchange.errors import ExchangeError, SymbolNotFoundError
from aetheris.adapters.exchange.http import ExchangeHttpClient
from aetheris.adapters.exchange.ports import MarketDataPort
from aetheris.core.config import BinanceFuturesSettings, MarketDataSettings
from aetheris.core.freshness import Observation, utcnow
from aetheris.core.logging import get_logger
from aetheris.domain.enums import ConnectionStatus, Timeframe
from aetheris.domain.market import (
    CandleSeries,
    ExchangeInfo,
    MarketDataStatus,
    Symbol,
    Ticker,
)

_log = get_logger("exchange.binance")

_EXCHANGE_INFO_KEY = "exchange-info"
_ALL_TICKERS_KEY = "all-tickers"


class _ConnectionTracker:
    """Records what actually happened upstream.

    The status it reports is derived only from observed outcomes. Having a base
    URL configured is not evidence of a connection, so before the first
    response the answer is UNKNOWN, not CONNECTED.
    """

    def __init__(self) -> None:
        self.last_success: datetime | None = None
        self.last_failure: datetime | None = None
        self.last_error_code: str | None = None

    def record_success(self) -> None:
        self.last_success = utcnow()

    def record_failure(self, error_code: str) -> None:
        self.last_failure = utcnow()
        self.last_error_code = error_code

    @property
    def status(self) -> ConnectionStatus:
        if self.last_success is None:
            return (
                ConnectionStatus.UNAVAILABLE
                if self.last_failure is not None
                else ConnectionStatus.UNKNOWN
            )
        if self.last_failure is not None and self.last_failure > self.last_success:
            # We have worked before and are failing now: degraded, not dead.
            return ConnectionStatus.DEGRADED
        return ConnectionStatus.CONNECTED


class BinanceFuturesMarketDataAdapter(MarketDataPort):
    """Public market data from Binance USDT-M Futures."""

    def __init__(
        self,
        *,
        settings: BinanceFuturesSettings,
        market_data: MarketDataSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._market_data = market_data
        self._tracker = _ConnectionTracker()
        self._universe_updated_at: datetime | None = None

        self._http = ExchangeHttpClient(
            base_url=settings.futures_rest_base_url,
            source=endpoints.SOURCE_REST,
            request_timeout_seconds=settings.request_timeout_seconds,
            connect_timeout_seconds=settings.connect_timeout_seconds,
            max_retries=settings.max_retries,
            backoff_seconds=settings.backoff_seconds,
            max_backoff_seconds=settings.max_backoff_seconds,
            transport=transport,
        )

        self._exchange_info_cache: TTLCache[ExchangeInfo] = TTLCache(
            ttl_seconds=settings.exchange_info_ttl_seconds, max_entries=1
        )
        self._ticker_cache: TTLCache[Ticker] = TTLCache(
            ttl_seconds=settings.ticker_ttl_seconds,
            max_entries=settings.ticker_cache_max_entries,
        )
        self._klines_cache: TTLCache[CandleSeries] = TTLCache(
            ttl_seconds=settings.klines_ttl_seconds,
            max_entries=settings.klines_cache_max_entries,
        )
        # One slot: there is exactly one whole-market snapshot at a time, and
        # it is large, so it must not be allowed to accumulate copies.
        self._all_tickers_cache: TTLCache[tuple[Ticker, ...]] = TTLCache(
            ttl_seconds=settings.ticker_ttl_seconds, max_entries=1
        )

    @property
    def name(self) -> str:
        return endpoints.EXCHANGE_NAME

    @property
    def source(self) -> str:
        return endpoints.SOURCE_REST

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Issue a request and keep the connection tracker truthful."""
        try:
            payload = await self._http.get_json(path, params)
        except ExchangeError as exc:
            self._tracker.record_failure(str(exc.code))
            raise
        self._tracker.record_success()
        return payload

    # ------------------------------------------------------------------
    # Exchange metadata and symbol discovery
    # ------------------------------------------------------------------

    async def get_exchange_info(self) -> ExchangeInfo:
        cached = self._exchange_info_cache.get(_EXCHANGE_INFO_KEY)
        if cached is not None:
            return cached

        payload = await self._get(endpoints.EXCHANGE_INFO)
        info = parsing.parse_exchange_info(payload)
        self._exchange_info_cache.set(_EXCHANGE_INFO_KEY, info)
        self._universe_updated_at = utcnow()

        eligible = sum(1 for s in info.symbols if parsing.is_eligible(s))
        _log.info(
            "symbol_universe_updated",
            source=self.source,
            symbols_discovered=info.symbol_count,
            eligible_symbols=eligible,
        )
        return info

    async def get_symbols(self, *, eligible_only: bool = True) -> tuple[Symbol, ...]:
        info = await self.get_exchange_info()
        if not eligible_only:
            return info.symbols
        return tuple(s for s in info.symbols if parsing.is_eligible(s))

    #: Binance serves per-symbol leverage brackets only from an authenticated
    #: endpoint, so Symbol.max_leverage stays None for public data. The
    #: leverage decision chain treats that as unknown and fails closed rather
    #: than assuming a ceiling. The path is deliberately not named here: the
    #: architecture test forbids any leverage endpoint string in the package,
    #: and that guard is worth more than the convenience of a reference.
    LEVERAGE_BRACKETS_REQUIRE_AUTH = True

    async def resolve_symbol(self, symbol: str) -> Symbol:
        """Look a symbol up in the discovered universe.

        Checking against discovered metadata before calling upstream means an
        unknown symbol costs one cached lookup instead of a round trip, and the
        caller gets a clean 404 rather than a venue error.
        """
        wanted = symbol.upper()
        info = await self.get_exchange_info()
        for candidate in info.symbols:
            if candidate.symbol == wanted:
                return candidate
        raise SymbolNotFoundError(
            f"Symbol {wanted} is not listed on {self.name}", details={"symbol": wanted}
        )

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def _classify(self, event_time: datetime | None, max_age_seconds: float) -> float | None:
        """Return the observation's age, or ``None`` when it cannot be judged."""
        if event_time is None:
            return None
        age = (utcnow() - event_time).total_seconds()
        # A venue clock slightly ahead of ours yields a small negative age;
        # treat that as fresh rather than as a fault.
        return max(age, 0.0)

    async def get_ticker(self, symbol: str) -> Observation[Ticker]:
        resolved = await self.resolve_symbol(symbol)
        key = f"ticker:{resolved.symbol}"

        ticker = self._ticker_cache.get(key)
        if ticker is None:
            params = {"symbol": resolved.symbol}
            # Best bid/ask lives on a different endpoint; fetch it alongside
            # rather than serially, and treat it as optional enrichment.
            results: tuple[Any, Any] = await asyncio.gather(
                self._get(endpoints.TICKER_24H, params),
                self._get(endpoints.BOOK_TICKER, params),
                return_exceptions=True,
            )
            stats_result, book_result = results[0], results[1]

            if isinstance(stats_result, BaseException):
                # The statistics are the ticker; without them there is nothing
                # truthful to return.
                raise stats_result

            book_payload: Any = book_result
            if isinstance(book_result, BaseException):
                _log.warning(
                    "exchange_error",
                    source=self.source,
                    path=endpoints.BOOK_TICKER,
                    symbol=resolved.symbol,
                    reason="book_ticker_unavailable",
                )
                book_payload = None

            ticker = parsing.parse_ticker(stats_result, book_payload)
            self._ticker_cache.set(key, ticker)

        age = self._classify(ticker.event_time, self._market_data.max_ticker_age_seconds)
        if age is not None and age > self._market_data.max_ticker_age_seconds:
            return Observation[Ticker].stale(
                source=self.source,
                detail=f"Ticker for {resolved.symbol} is {age:.1f}s old",
                event_ts=ticker.event_time,
            )
        return Observation[Ticker].ok(ticker, source=self.source, event_ts=ticker.event_time)

    async def get_all_tickers(self) -> Observation[tuple[Ticker, ...]]:
        """Whole-market snapshot: two requests, not two per instrument.

        Snapshot-level freshness is judged on the *newest* event timestamp,
        which answers "is this feed alive at all". It deliberately does not
        answer "is this instrument's price current": in a 500-contract snapshot
        a quiet perpetual is routinely minutes behind, and judging the whole on
        the oldest would mark the entire market stale forever while blanking
        every price on the table.

        Per-instrument freshness is therefore decided by the consumer, per row,
        against each ticker's own timestamp -- so a stale quiet contract and a
        live BTC quote can sit in the same response, each labelled correctly.
        """
        cached = self._all_tickers_cache.get(_ALL_TICKERS_KEY)
        if cached is None:
            results: tuple[Any, Any] = await asyncio.gather(
                self._get(endpoints.TICKER_24H),
                self._get(endpoints.BOOK_TICKER),
                return_exceptions=True,
            )
            stats_result, book_result = results[0], results[1]
            if isinstance(stats_result, BaseException):
                raise stats_result

            book_payload: Any = book_result
            if isinstance(book_result, BaseException):
                _log.warning(
                    "exchange_error",
                    source=self.source,
                    path=endpoints.BOOK_TICKER,
                    reason="book_ticker_snapshot_unavailable",
                )
                book_payload = None

            cached = parsing.parse_ticker_list(stats_result, book_payload)
            self._all_tickers_cache.set(_ALL_TICKERS_KEY, cached)
            # 'event' is structlog's own key for the event name, so the
            # discriminator here is 'kind'.
            _log.info(
                "market_data_updated",
                source=self.source,
                kind="ticker_snapshot",
                tickers=len(cached),
            )

        event_times = [t.event_time for t in cached if t.event_time is not None]
        newest = max(event_times) if event_times else None
        age = self._classify(newest, self._market_data.max_ticker_age_seconds)
        if age is not None and age > self._market_data.max_ticker_age_seconds:
            # Even the most recently traded instrument is old: the feed itself
            # has stopped, which is a real outage rather than a quiet market.
            return Observation[tuple[Ticker, ...]].stale(
                source=self.source,
                detail=f"Newest ticker in the whole snapshot is {age:.1f}s old",
                event_ts=newest,
            )
        return Observation[tuple[Ticker, ...]].ok(cached, source=self.source, event_ts=newest)

    async def get_klines(
        self, symbol: str, timeframe: Timeframe, *, limit: int
    ) -> Observation[CandleSeries]:
        resolved = await self.resolve_symbol(symbol)
        bounded = max(1, min(limit, self._settings.max_klines_limit))
        key = f"klines:{resolved.symbol}:{timeframe.value}:{bounded}"

        series = self._klines_cache.get(key)
        if series is None:
            payload = await self._get(
                endpoints.KLINES,
                {"symbol": resolved.symbol, "interval": timeframe.value, "limit": bounded},
            )
            series = parsing.parse_klines(payload, symbol=resolved.symbol, timeframe=timeframe)
            self._klines_cache.set(key, series)

        last_close = series.last_close_time
        if last_close is None:
            # An empty series is a legitimate answer for a just-listed symbol.
            return Observation[CandleSeries].ok(series, source=self.source)

        # The most recent bar is usually still forming, so its close time lies
        # in the future and the series is as fresh as it can be.
        age = self._classify(last_close, 0.0) or 0.0
        max_age = timeframe.seconds * self._market_data.max_candle_age_multiple
        if age > max_age:
            return Observation[CandleSeries].stale(
                source=self.source,
                detail=(
                    f"Most recent {timeframe.value} candle for {resolved.symbol} "
                    f"closed {age:.0f}s ago (limit {max_age:.0f}s)"
                ),
                event_ts=last_close,
            )
        return Observation[CandleSeries].ok(series, source=self.source, event_ts=last_close)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_market_status(self) -> MarketDataStatus:
        """Report observed connectivity without provoking a request.

        Reads only what is already cached: a status endpoint that triggers an
        upstream call would turn a monitoring poll into rate-limit pressure.
        """
        info = self._exchange_info_cache.get(_EXCHANGE_INFO_KEY)
        last_success = self._tracker.last_success
        return MarketDataStatus(
            exchange=self.name,
            source=self.source,
            connection_status=self._tracker.status,
            last_success=last_success,
            last_failure=self._tracker.last_failure,
            last_error_code=self._tracker.last_error_code,
            last_success_age_seconds=(
                (utcnow() - last_success).total_seconds() if last_success else None
            ),
            symbols_discovered=info.symbol_count if info else None,
            eligible_symbols=(
                sum(1 for s in info.symbols if parsing.is_eligible(s)) if info else None
            ),
            universe_updated_at=self._universe_updated_at,
        )
