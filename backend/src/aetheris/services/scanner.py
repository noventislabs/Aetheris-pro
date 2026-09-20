"""Market scanner orchestration.

The scanner's whole design problem is cost. The eligible universe is several
hundred perpetuals, and a naive implementation fetches a ticker and a candle
series per instrument per request -- roughly a thousand upstream calls for one
page of a table. That is a rate-limit ban, not a performance issue.

So the work is split by what each field actually costs:

* **Ticker fields** (price, 24h change, volume) come from *one* whole-market
  request, so they can be filtered and sorted across the entire universe for
  free.
* **Candle-derived fields** (volatility, ATR, momentum, trend, opportunity
  score) cost one request per instrument, so they are computed for a bounded
  set only, with bounded concurrency.

That produces two honest modes, and the response says which one ran:

``FULL_UNIVERSE``
    Ordering by a ticker field. Every eligible instrument was ranked.

``LIQUIDITY_POOL``
    Ordering by (or filtering on) a candle-derived field. Metrics were computed
    for the most liquid N instruments and the ranking covers that pool. The
    response reports the pool size so a reader is never told a subset is the
    whole market.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Final

from aetheris.adapters.exchange.errors import ExchangeError
from aetheris.adapters.exchange.ports import MarketDataPort
from aetheris.analysis.metrics import compute_metrics
from aetheris.analysis.scoring import score_opportunity
from aetheris.core.config import MarketDataSettings, ScannerSettings
from aetheris.core.freshness import DataStatus, Observation, utcnow
from aetheris.core.logging import get_logger
from aetheris.domain.enums import Timeframe
from aetheris.domain.market import Symbol, Ticker
from aetheris.domain.scanner import (
    MetricStatus,
    RankingScope,
    ScannerMetrics,
    ScannerPage,
    ScannerRow,
    TrendDirection,
)

_log = get_logger("scanner")

#: Sort fields answerable from the whole-market ticker snapshot.
TICKER_SORT_FIELDS: Final = frozenset(
    {
        "symbol",
        "last_price",
        "price_change_percent_24h",
        "volume_24h",
        "quote_volume_24h",
    }
)

#: Sort fields that require candles, and therefore a bounded pool.
METRIC_SORT_FIELDS: Final = frozenset(
    {
        "volatility_percent",
        "momentum_percent",
        "atr_percent",
        "relative_volume",
        "trend_consistency",
        "opportunity_score",
    }
)

SORT_FIELDS: Final = TICKER_SORT_FIELDS | METRIC_SORT_FIELDS

_TICKER_ACCESSORS: Final[dict[str, Callable[[ScannerRow], Decimal | str | None]]] = {
    "symbol": lambda r: r.symbol,
    "last_price": lambda r: r.last_price,
    "price_change_percent_24h": lambda r: r.price_change_percent_24h,
    "volume_24h": lambda r: r.volume_24h,
    "quote_volume_24h": lambda r: r.quote_volume_24h,
}

_METRIC_ACCESSORS: Final[dict[str, Callable[[ScannerRow], Decimal | None]]] = {
    "volatility_percent": lambda r: r.metrics.volatility_percent if r.metrics else None,
    "momentum_percent": lambda r: r.metrics.momentum_percent if r.metrics else None,
    "atr_percent": lambda r: r.metrics.atr_percent if r.metrics else None,
    "relative_volume": lambda r: r.metrics.relative_volume if r.metrics else None,
    "trend_consistency": lambda r: r.metrics.trend_consistency if r.metrics else None,
    "opportunity_score": lambda r: r.opportunity.score if r.opportunity else None,
}


@dataclass(frozen=True, slots=True)
class ScanFilters:
    """Filters applied before ranking. All optional, all bounded by the API."""

    search: str | None = None
    quote_asset: str | None = None
    min_quote_volume: Decimal | None = None
    min_price_change_percent: Decimal | None = None
    max_price_change_percent: Decimal | None = None
    #: Candle-derived filters force the bounded-pool path.
    min_volatility_percent: Decimal | None = None
    min_relative_volume: Decimal | None = None
    trend: TrendDirection | None = None

    @property
    def needs_metrics(self) -> bool:
        return (
            self.min_volatility_percent is not None
            or self.min_relative_volume is not None
            or self.trend is not None
        )


@dataclass(frozen=True, slots=True)
class ScanQuery:
    """A fully-validated scan request."""

    sort_by: str = "quote_volume_24h"
    descending: bool = True
    page: int = 1
    page_size: int = 25
    timeframe: Timeframe = Timeframe.H1
    include_metrics: bool = False
    filters: ScanFilters = field(default_factory=ScanFilters)

    @property
    def sorts_by_metric(self) -> bool:
        return self.sort_by in METRIC_SORT_FIELDS


class ScannerService:
    """Builds scanner pages from the read-only market-data layer.

    Holds no state between requests beyond what the exchange adapter caches,
    and places no orders: it depends on :class:`MarketDataPort`, which has no
    execution methods.
    """

    def __init__(
        self,
        exchange: MarketDataPort,
        settings: ScannerSettings,
        market_data: MarketDataSettings,
    ) -> None:
        self._exchange = exchange
        self._settings = settings
        self._market_data = market_data

    async def scan(self, query: ScanQuery) -> ScannerPage:
        started = utcnow()
        universe = await self._exchange.get_symbols(eligible_only=True)
        ticker_observation = await self._exchange.get_all_tickers()

        # A stale or unavailable snapshot yields rows with metadata and no
        # prices, each carrying the reason -- not rows with zeros in them.
        tickers_by_symbol: dict[str, Ticker] = {}
        if ticker_observation.value is not None:
            tickers_by_symbol = {t.symbol: t for t in ticker_observation.value}

        now = utcnow()
        rows = [
            self._build_row(symbol, tickers_by_symbol.get(symbol.symbol), ticker_observation, now)
            for symbol in universe
        ]
        rows = [row for row in rows if self._passes_ticker_filters(row, query.filters)]

        needs_pool = query.sorts_by_metric or query.filters.needs_metrics
        if needs_pool:
            rows, pool_size = await self._rank_with_metrics(rows, query)
            scope = RankingScope.LIQUIDITY_POOL
        else:
            rows = self._sort(rows, query.sort_by, descending=query.descending)
            pool_size = None
            scope = RankingScope.FULL_UNIVERSE

        total = len(rows)
        page_rows = self._paginate(rows, query.page, query.page_size)

        # In full-universe mode metrics are an optional extra, computed only
        # for the rows actually being returned.
        if query.include_metrics and not needs_pool:
            page_rows = await self._attach_metrics(page_rows, query.timeframe)

        return ScannerPage(
            rows=tuple(page_rows),
            page=query.page,
            page_size=query.page_size,
            total_rows=total,
            universe_size=len(universe),
            ranking_scope=scope,
            candidate_pool_size=pool_size,
            sort_by=query.sort_by,
            direction="desc" if query.descending else "asc",
            ticker_source=ticker_observation.source,
            ticker_status=ticker_observation.status,
            ticker_event_ts=(
                ticker_observation.event_ts.isoformat() if ticker_observation.event_ts else None
            ),
            ticker_age_seconds=ticker_observation.age_seconds,
            scanned_at=started.isoformat(),
        )

    # ------------------------------------------------------------------
    # Row construction
    # ------------------------------------------------------------------

    def _build_row(
        self,
        symbol: Symbol,
        ticker: Ticker | None,
        observation: Observation[tuple[Ticker, ...]],
        now: datetime,
    ) -> ScannerRow:
        """Merge venue metadata with this instrument's own ticker.

        Freshness is decided per instrument. A thinly traded perpetual whose
        last trade was an hour ago is STALE and carries no prices, while an
        actively traded one in the same snapshot is OK -- neither borrows the
        other's credibility.
        """
        if ticker is None:
            # Either the instrument is listed but missing from the snapshot, or
            # the snapshot itself was stale. Either way the row keeps its
            # metadata and carries no prices -- never zeros.
            status = (
                observation.status
                if observation.status is not DataStatus.OK
                else DataStatus.UNAVAILABLE
            )
            return ScannerRow(
                symbol=symbol.symbol,
                base_asset=symbol.base_asset,
                quote_asset=symbol.quote_asset,
                contract_type=symbol.contract_type,
                status=symbol.status,
                ticker_status=status,
                ticker_detail=(observation.detail or "No ticker for this symbol in the snapshot"),
            )
        age = (now - ticker.event_time).total_seconds() if ticker.event_time is not None else None
        if age is not None and age > self._market_data.max_ticker_age_seconds:
            return ScannerRow(
                symbol=symbol.symbol,
                base_asset=symbol.base_asset,
                quote_asset=symbol.quote_asset,
                contract_type=symbol.contract_type,
                status=symbol.status,
                ticker_status=DataStatus.STALE,
                ticker_detail=f"Last trade for this instrument was {age:.0f}s ago",
            )

        return ScannerRow(
            symbol=symbol.symbol,
            base_asset=symbol.base_asset,
            quote_asset=symbol.quote_asset,
            contract_type=symbol.contract_type,
            status=symbol.status,
            ticker_status=DataStatus.OK,
            last_price=ticker.last_price,
            bid_price=ticker.bid_price,
            ask_price=ticker.ask_price,
            price_change_24h=ticker.price_change_24h,
            price_change_percent_24h=ticker.price_change_percent_24h,
            high_24h=ticker.high_24h,
            low_24h=ticker.low_24h,
            volume_24h=ticker.volume_24h,
            quote_volume_24h=ticker.quote_volume_24h,
        )

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    @staticmethod
    def _passes_ticker_filters(row: ScannerRow, filters: ScanFilters) -> bool:
        """Filters answerable without candles.

        A row missing the value a filter tests is excluded rather than kept:
        "volume at least X" cannot be satisfied by an unknown volume, and
        treating unknown as zero would be a fabricated comparison either way.
        """
        if filters.search:
            needle = filters.search.upper()
            if needle not in row.symbol and not row.base_asset.startswith(needle):
                return False
        if filters.quote_asset and row.quote_asset != filters.quote_asset:
            return False
        if filters.min_quote_volume is not None and (
            row.quote_volume_24h is None or row.quote_volume_24h < filters.min_quote_volume
        ):
            return False
        if filters.min_price_change_percent is not None and (
            row.price_change_percent_24h is None
            or row.price_change_percent_24h < filters.min_price_change_percent
        ):
            return False
        # Kept as a guard clause like the checks above it rather than folded
        # into the return: a uniform chain of "reject if" tests reads better
        # here than one negated compound expression at the end.
        if filters.max_price_change_percent is not None and (  # noqa: SIM103
            row.price_change_percent_24h is None
            or row.price_change_percent_24h > filters.max_price_change_percent
        ):
            return False
        return True

    @staticmethod
    def _passes_metric_filters(row: ScannerRow, filters: ScanFilters) -> bool:
        metrics = row.metrics
        if metrics is None:
            return False
        if (
            filters.min_volatility_percent is not None
            and metrics.volatility_percent < filters.min_volatility_percent
        ):
            return False
        if filters.min_relative_volume is not None and (
            metrics.relative_volume is None or metrics.relative_volume < filters.min_relative_volume
        ):
            return False
        return not (filters.trend is not None and metrics.trend is not filters.trend)

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    async def _rank_with_metrics(
        self, rows: list[ScannerRow], query: ScanQuery
    ) -> tuple[list[ScannerRow], int]:
        """Compute metrics for the most liquid slice, then rank within it."""
        pool_size = min(self._settings.candidate_pool_size, self._settings.max_metric_symbols)
        candidates = self._sort(rows, "quote_volume_24h", descending=True)[:pool_size]

        scored = await self._attach_metrics(candidates, query.timeframe)
        if query.filters.needs_metrics:
            scored = [r for r in scored if self._passes_metric_filters(r, query.filters)]

        return self._sort(scored, query.sort_by, descending=query.descending), len(candidates)

    async def _attach_metrics(
        self, rows: list[ScannerRow], timeframe: Timeframe
    ) -> list[ScannerRow]:
        """Fetch candles and compute metrics, with bounded concurrency.

        A per-symbol failure degrades that row to UNAVAILABLE with a reason; it
        never fails the scan. One delisted-mid-scan instrument must not take
        the whole table down.
        """
        if not rows:
            return rows

        capped = rows[: self._settings.max_metric_symbols]
        semaphore = asyncio.Semaphore(self._settings.metric_concurrency)
        now = utcnow()

        async def enrich(row: ScannerRow) -> ScannerRow:
            async with semaphore:
                try:
                    observation = await self._exchange.get_klines(
                        row.symbol, timeframe, limit=self._settings.candle_limit
                    )
                except ExchangeError as exc:
                    return row.model_copy(
                        update={
                            "metrics_status": MetricStatus.UNAVAILABLE,
                            "metrics_detail": f"{exc.code}: candles could not be retrieved",
                        }
                    )

            if observation.value is None:
                return row.model_copy(
                    update={
                        "metrics_status": MetricStatus.UNAVAILABLE,
                        "metrics_detail": observation.detail
                        or f"Candles unavailable ({observation.status})",
                    }
                )

            metrics: ScannerMetrics | None = compute_metrics(observation.value, now)
            if metrics is None:
                return row.model_copy(
                    update={
                        "metrics_status": MetricStatus.INSUFFICIENT_DATA,
                        "metrics_detail": ("Not enough closed candles to compute statistics"),
                    }
                )

            return row.model_copy(
                update={
                    "metrics_status": MetricStatus.CALCULATED,
                    "metrics": metrics,
                    "opportunity": score_opportunity(metrics),
                }
            )

        enriched = await asyncio.gather(*(enrich(row) for row in capped))
        _log.info(
            "market_data_updated",
            kind="scanner_metrics",
            symbols=len(capped),
            timeframe=timeframe.value,
        )
        # Rows beyond the cap keep NOT_REQUESTED rather than being dropped.
        return [*enriched, *rows[self._settings.max_metric_symbols :]]

    # ------------------------------------------------------------------
    # Ordering and paging
    # ------------------------------------------------------------------

    @staticmethod
    def _sort(rows: Iterable[ScannerRow], sort_by: str, *, descending: bool) -> list[ScannerRow]:
        """Order rows, keeping unavailable values out of the ranking.

        Rows whose sort value is unknown always land last, in both directions,
        and are ordered among themselves by symbol. They are never coerced to
        zero -- doing so would rank an instrument with no data above one with a
        genuine negative value, which is a fabricated comparison.
        """
        accessor = _TICKER_ACCESSORS.get(sort_by) or _METRIC_ACCESSORS.get(sort_by)
        if accessor is None:  # pragma: no cover - API validates first
            raise ValueError(f"unsupported sort field {sort_by!r}")

        materialised = list(rows)
        present = [r for r in materialised if accessor(r) is not None]
        absent = [r for r in materialised if accessor(r) is None]

        if sort_by == "symbol":
            present.sort(key=lambda r: r.symbol, reverse=descending)
        else:
            # Symbol is the tie-break, so equal values order identically on
            # every call and pagination stays stable between pages.
            present.sort(key=lambda r: r.symbol)
            present.sort(
                key=lambda r: _as_decimal(accessor(r)),
                reverse=descending,
            )
        absent.sort(key=lambda r: r.symbol)
        return present + absent

    @staticmethod
    def _paginate(rows: list[ScannerRow], page: int, page_size: int) -> list[ScannerRow]:
        start = (page - 1) * page_size
        return rows[start : start + page_size]


def _as_decimal(value: Decimal | str | None) -> Decimal:
    """Narrow an accessor result for numeric sorting.

    Only called for rows already known to have a value, and only for numeric
    fields; the symbol field is sorted on its own path.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(0)  # pragma: no cover - unreachable via the sort paths
