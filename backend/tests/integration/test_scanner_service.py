"""Scanner behaviour against mocked upstream responses.

Exercised through the real Binance adapter so the whole path is covered:
bulk ticker snapshot, bounded candle fetching, metrics, scoring, filtering,
ordering and paging. No test here touches the network.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tests.fixtures import binance_payloads as payloads
from tests.fixtures.transport import RoutingHandler, json_route, status_route

from aetheris.adapters.exchange.binance import endpoints
from aetheris.adapters.exchange.binance.adapter import BinanceFuturesMarketDataAdapter
from aetheris.adapters.exchange.errors import ExchangeUnavailableError
from aetheris.core.config import BinanceFuturesSettings, MarketDataSettings, ScannerSettings
from aetheris.core.freshness import DataStatus
from aetheris.domain.scanner import MetricStatus, RankingScope, TrendDirection
from aetheris.services.scanner import ScanFilters, ScannerService, ScanQuery

UNIVERSE = ["BTCUSDT", "ETHUSDT", "0GUSDT"]


def build_service(
    handler: RoutingHandler, **scanner_overrides: object
) -> tuple[ScannerService, BinanceFuturesMarketDataAdapter]:
    adapter = BinanceFuturesMarketDataAdapter(
        settings=BinanceFuturesSettings(
            _env_file=None,  # type: ignore[call-arg]
            max_retries=0,
            backoff_seconds=0.0,
            max_backoff_seconds=0.0,
        ),
        market_data=MarketDataSettings(_env_file=None),  # type: ignore[call-arg]
        transport=handler.transport(),
    )
    settings = ScannerSettings(_env_file=None, **scanner_overrides)  # type: ignore[call-arg, arg-type]
    market_data = MarketDataSettings(_env_file=None)  # type: ignore[call-arg]
    return ScannerService(adapter, settings, market_data), adapter


def routes(
    *,
    quote_volumes: dict[str, str] | None = None,
    change_percents: dict[str, str] | None = None,
    ticker_age: float = 1.0,
    candle_count: int = 40,
) -> dict[str, object]:
    return {
        endpoints.EXCHANGE_INFO: json_route(payloads.exchange_info()),
        endpoints.TICKER_24H: json_route(
            payloads.ticker_24h_list(
                UNIVERSE,
                age_seconds=ticker_age,
                quote_volumes=quote_volumes,
                change_percents=change_percents,
            )
        ),
        endpoints.BOOK_TICKER: json_route(payloads.book_ticker_list(UNIVERSE)),
        endpoints.KLINES: json_route(payloads.klines(count=candle_count)),
    }


def handler(**kwargs: object) -> RoutingHandler:
    return RoutingHandler(routes(**kwargs))  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Universe and provenance
# ----------------------------------------------------------------------


async def test_scan_covers_the_discovered_eligible_universe() -> None:
    service, adapter = build_service(handler())
    try:
        page = await service.scan(ScanQuery())
        # Three eligible perpetuals from the fixture universe of six.
        assert page.universe_size == 3
        assert {r.symbol for r in page.rows} == set(UNIVERSE)
    finally:
        await adapter.aclose()


async def test_one_bulk_request_serves_the_whole_universe() -> None:
    """The scan must not cost one ticker request per instrument."""
    routing = handler()
    service, adapter = build_service(routing)
    try:
        await service.scan(ScanQuery())
        assert routing.count(endpoints.TICKER_24H) == 1
        assert routing.count(endpoints.EXCHANGE_INFO) == 1
    finally:
        await adapter.aclose()


async def test_scan_reports_ticker_provenance() -> None:
    service, adapter = build_service(handler())
    try:
        page = await service.scan(ScanQuery())
        assert page.ticker_source == "binance-futures-usdm:rest"
        assert page.ticker_status is DataStatus.OK
        assert page.ticker_event_ts is not None
        assert page.scanned_at
    finally:
        await adapter.aclose()


async def test_no_metrics_requested_means_no_candle_calls() -> None:
    routing = handler()
    service, adapter = build_service(routing)
    try:
        page = await service.scan(ScanQuery())
        assert routing.count(endpoints.KLINES) == 0
        assert all(r.metrics_status is MetricStatus.NOT_REQUESTED for r in page.rows)
        assert all(r.metrics is None for r in page.rows)
    finally:
        await adapter.aclose()


# ----------------------------------------------------------------------
# Stale and unavailable upstream data
# ----------------------------------------------------------------------


async def test_stale_snapshot_yields_rows_with_no_prices() -> None:
    """Stale data must not be served as current numbers."""
    service, adapter = build_service(handler(ticker_age=900))
    try:
        page = await service.scan(ScanQuery())
        assert page.ticker_status is DataStatus.STALE
        assert page.rows  # metadata rows still exist
        for row in page.rows:
            assert row.last_price is None
            assert row.ticker_status is not DataStatus.OK
            assert row.ticker_detail
    finally:
        await adapter.aclose()


async def test_symbol_absent_from_the_snapshot_is_reported_not_zeroed() -> None:
    routing = handler()
    routing.routes[endpoints.TICKER_24H] = json_route(
        payloads.ticker_24h_list(["BTCUSDT"])  # ETH and 0G missing
    )
    service, adapter = build_service(routing)
    try:
        page = await service.scan(ScanQuery())
        by_symbol = {r.symbol: r for r in page.rows}
        assert by_symbol["BTCUSDT"].last_price is not None
        assert by_symbol["ETHUSDT"].last_price is None
        assert by_symbol["ETHUSDT"].ticker_status is DataStatus.UNAVAILABLE
        assert by_symbol["ETHUSDT"].ticker_detail
    finally:
        await adapter.aclose()


async def test_candle_failure_degrades_only_that_row() -> None:
    routing = handler()
    routing.routes[endpoints.KLINES] = status_route(503)
    service, adapter = build_service(routing)
    try:
        page = await service.scan(ScanQuery(include_metrics=True))
        assert len(page.rows) == 3  # the scan survives
        for row in page.rows:
            assert row.metrics_status is MetricStatus.UNAVAILABLE
            assert row.metrics is None
            assert row.metrics_detail
    finally:
        await adapter.aclose()


async def test_insufficient_candles_is_distinct_from_unavailable() -> None:
    routing = handler(candle_count=5)
    service, adapter = build_service(routing)
    try:
        page = await service.scan(ScanQuery(include_metrics=True))
        for row in page.rows:
            assert row.metrics_status is MetricStatus.INSUFFICIENT_DATA
            assert row.metrics is None
            assert row.opportunity is None
    finally:
        await adapter.aclose()


# ----------------------------------------------------------------------
# Metrics and scoring
# ----------------------------------------------------------------------


async def test_metrics_and_score_are_attached_when_requested() -> None:
    service, adapter = build_service(handler())
    try:
        page = await service.scan(ScanQuery(include_metrics=True))
        for row in page.rows:
            assert row.metrics_status is MetricStatus.CALCULATED
            assert row.metrics is not None
            assert row.metrics.candles_used >= 15
            assert row.opportunity is not None
            assert Decimal(0) <= row.opportunity.score <= Decimal(100)
            assert len(row.opportunity.components) == 4
    finally:
        await adapter.aclose()


async def test_metric_work_is_capped_per_request() -> None:
    """A hard ceiling on candle requests, regardless of page size."""
    routing = handler()
    service, adapter = build_service(routing, max_metric_symbols=1)
    try:
        page = await service.scan(ScanQuery(include_metrics=True, page_size=100))
        assert routing.count(endpoints.KLINES) == 1
        calculated = [r for r in page.rows if r.metrics_status is MetricStatus.CALCULATED]
        not_requested = [r for r in page.rows if r.metrics_status is MetricStatus.NOT_REQUESTED]
        assert len(calculated) == 1
        # Rows beyond the cap are kept and honestly labelled, not dropped.
        assert len(not_requested) == 2
    finally:
        await adapter.aclose()


async def test_metrics_only_fetched_for_the_returned_page() -> None:
    routing = handler()
    service, adapter = build_service(routing)
    try:
        await service.scan(ScanQuery(include_metrics=True, page_size=1))
        assert routing.count(endpoints.KLINES) == 1
    finally:
        await adapter.aclose()


# ----------------------------------------------------------------------
# Ranking scope
# ----------------------------------------------------------------------


async def test_ticker_sort_ranks_the_full_universe() -> None:
    service, adapter = build_service(handler())
    try:
        page = await service.scan(ScanQuery(sort_by="quote_volume_24h"))
        assert page.ranking_scope is RankingScope.FULL_UNIVERSE
        assert page.candidate_pool_size is None
    finally:
        await adapter.aclose()


async def test_metric_sort_declares_a_bounded_pool() -> None:
    """A subset ranking must never be presented as the whole market."""
    service, adapter = build_service(handler())
    try:
        page = await service.scan(ScanQuery(sort_by="opportunity_score"))
        assert page.ranking_scope is RankingScope.LIQUIDITY_POOL
        assert page.candidate_pool_size == 3
    finally:
        await adapter.aclose()


async def test_metric_filter_also_forces_the_pool_path() -> None:
    service, adapter = build_service(handler())
    try:
        page = await service.scan(
            ScanQuery(filters=ScanFilters(min_volatility_percent=Decimal("0")))
        )
        assert page.ranking_scope is RankingScope.LIQUIDITY_POOL
    finally:
        await adapter.aclose()


async def test_pool_is_bounded_by_configuration() -> None:
    routing = handler()
    service, adapter = build_service(routing, candidate_pool_size=2)
    try:
        page = await service.scan(ScanQuery(sort_by="opportunity_score"))
        assert page.candidate_pool_size == 2
        assert routing.count(endpoints.KLINES) == 2
    finally:
        await adapter.aclose()


# ----------------------------------------------------------------------
# Sorting
# ----------------------------------------------------------------------


async def test_sort_by_quote_volume_descending_and_ascending() -> None:
    volumes = {"BTCUSDT": "900", "ETHUSDT": "500", "0GUSDT": "100"}
    service, adapter = build_service(handler(quote_volumes=volumes))
    try:
        desc = await service.scan(ScanQuery(sort_by="quote_volume_24h", descending=True))
        assert [r.symbol for r in desc.rows] == ["BTCUSDT", "ETHUSDT", "0GUSDT"]

        asc = await service.scan(ScanQuery(sort_by="quote_volume_24h", descending=False))
        assert [r.symbol for r in asc.rows] == ["0GUSDT", "ETHUSDT", "BTCUSDT"]
    finally:
        await adapter.aclose()


async def test_sort_by_symbol() -> None:
    service, adapter = build_service(handler())
    try:
        page = await service.scan(ScanQuery(sort_by="symbol", descending=False))
        assert [r.symbol for r in page.rows] == ["0GUSDT", "BTCUSDT", "ETHUSDT"]
    finally:
        await adapter.aclose()


async def test_unavailable_values_sort_last_in_both_directions() -> None:
    """Unknown must never be treated as zero, in either direction."""
    routing = handler(quote_volumes={"BTCUSDT": "900", "ETHUSDT": "500"})
    routing.routes[endpoints.TICKER_24H] = json_route(
        payloads.ticker_24h_list(
            ["BTCUSDT", "ETHUSDT"], quote_volumes={"BTCUSDT": "900", "ETHUSDT": "500"}
        )
    )  # 0GUSDT has no ticker at all
    service, adapter = build_service(routing)
    try:
        desc = await service.scan(ScanQuery(sort_by="quote_volume_24h", descending=True))
        assert [r.symbol for r in desc.rows] == ["BTCUSDT", "ETHUSDT", "0GUSDT"]

        asc = await service.scan(ScanQuery(sort_by="quote_volume_24h", descending=False))
        # 0GUSDT stays last: a missing value is not a small value.
        assert [r.symbol for r in asc.rows] == ["ETHUSDT", "BTCUSDT", "0GUSDT"]
    finally:
        await adapter.aclose()


async def test_ties_break_on_symbol_so_paging_is_stable() -> None:
    service, adapter = build_service(handler())  # all volumes identical
    try:
        first = await service.scan(ScanQuery(sort_by="quote_volume_24h"))
        second = await service.scan(ScanQuery(sort_by="quote_volume_24h"))
        assert [r.symbol for r in first.rows] == [r.symbol for r in second.rows]
    finally:
        await adapter.aclose()


# ----------------------------------------------------------------------
# Filtering, search and paging
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("search", "expected"),
    [("BTC", {"BTCUSDT"}), ("0G", {"0GUSDT"}), ("eth", {"ETHUSDT"}), ("ZZZ", set())],
)
async def test_search(search: str, expected: set[str]) -> None:
    service, adapter = build_service(handler())
    try:
        page = await service.scan(ScanQuery(filters=ScanFilters(search=search)))
        assert {r.symbol for r in page.rows} == expected
    finally:
        await adapter.aclose()


async def test_min_quote_volume_filter() -> None:
    volumes = {"BTCUSDT": "900", "ETHUSDT": "500", "0GUSDT": "100"}
    service, adapter = build_service(handler(quote_volumes=volumes))
    try:
        page = await service.scan(ScanQuery(filters=ScanFilters(min_quote_volume=Decimal("400"))))
        assert {r.symbol for r in page.rows} == {"BTCUSDT", "ETHUSDT"}
        assert page.total_rows == 2
    finally:
        await adapter.aclose()


async def test_rows_missing_the_filtered_value_are_excluded() -> None:
    """ "At least X" cannot be satisfied by an unknown value."""
    routing = handler()
    routing.routes[endpoints.TICKER_24H] = json_route(payloads.ticker_24h_list(["BTCUSDT"]))
    service, adapter = build_service(routing)
    try:
        page = await service.scan(ScanQuery(filters=ScanFilters(min_quote_volume=Decimal("0"))))
        assert {r.symbol for r in page.rows} == {"BTCUSDT"}
    finally:
        await adapter.aclose()


async def test_price_change_range_filter() -> None:
    changes = {"BTCUSDT": "10.0", "ETHUSDT": "1.0", "0GUSDT": "-5.0"}
    service, adapter = build_service(handler(change_percents=changes))
    try:
        page = await service.scan(
            ScanQuery(
                filters=ScanFilters(
                    min_price_change_percent=Decimal("0"),
                    max_price_change_percent=Decimal("5"),
                )
            )
        )
        assert {r.symbol for r in page.rows} == {"ETHUSDT"}
    finally:
        await adapter.aclose()


async def test_trend_filter_uses_real_candle_direction() -> None:
    service, adapter = build_service(handler())
    try:
        page = await service.scan(ScanQuery(filters=ScanFilters(trend=TrendDirection.UP)))
        # The fixture candles are flat, so nothing trends up. An empty result
        # is the truthful answer, not a reason to loosen the predicate.
        assert page.rows == ()
        assert page.ranking_scope is RankingScope.LIQUIDITY_POOL
    finally:
        await adapter.aclose()


async def test_pagination_splits_without_overlap() -> None:
    volumes = {"BTCUSDT": "900", "ETHUSDT": "500", "0GUSDT": "100"}
    service, adapter = build_service(handler(quote_volumes=volumes))
    try:
        first = await service.scan(ScanQuery(page=1, page_size=2))
        second = await service.scan(ScanQuery(page=2, page_size=2))
        assert [r.symbol for r in first.rows] == ["BTCUSDT", "ETHUSDT"]
        assert [r.symbol for r in second.rows] == ["0GUSDT"]
        assert first.total_rows == second.total_rows == 3
    finally:
        await adapter.aclose()


async def test_page_beyond_the_end_is_empty_not_an_error() -> None:
    service, adapter = build_service(handler())
    try:
        page = await service.scan(ScanQuery(page=50, page_size=25))
        assert page.rows == ()
        assert page.total_rows == 3
    finally:
        await adapter.aclose()


async def test_empty_universe_produces_an_empty_page() -> None:
    routing = RoutingHandler(
        {
            **routes(),  # type: ignore[arg-type]
            endpoints.EXCHANGE_INFO: json_route(payloads.exchange_info([])),
        }
    )
    service, adapter = build_service(routing)
    try:
        page = await service.scan(ScanQuery())
        assert page.rows == ()
        assert page.universe_size == 0
        assert page.total_rows == 0
    finally:
        await adapter.aclose()


async def test_upstream_failure_propagates_as_a_typed_exchange_error() -> None:
    """A dead venue surfaces as EXCHANGE_UNAVAILABLE, not an empty table.

    Returning zero rows would read as "no instruments match", which is a
    different and misleading claim.
    """
    routing = RoutingHandler({endpoints.EXCHANGE_INFO: status_route(503)})
    service, adapter = build_service(routing)
    try:
        with pytest.raises(ExchangeUnavailableError) as exc:
            await service.scan(ScanQuery())
        assert exc.value.code == "EXCHANGE_UNAVAILABLE"
    finally:
        await adapter.aclose()


async def test_freshness_is_judged_per_instrument_not_per_snapshot() -> None:
    """A quiet contract must not blank the whole market.

    Regression guard for a real defect: snapshot freshness was judged on the
    oldest ticker, so one thinly traded perpetual -- always minutes behind in a
    500-contract snapshot -- marked every row stale and blanked 528 prices at
    once, verified against live Binance.
    """
    routing = handler()
    routing.routes[endpoints.TICKER_24H] = json_route(
        payloads.ticker_24h_list(
            UNIVERSE,
            ages={"BTCUSDT": 2, "ETHUSDT": 3, "0GUSDT": 4000},
        )
    )
    service, adapter = build_service(routing)
    try:
        page = await service.scan(ScanQuery())
        by_symbol = {r.symbol: r for r in page.rows}

        # The feed is alive, so the snapshot as a whole is OK.
        assert page.ticker_status is DataStatus.OK

        # Actively traded instruments keep their prices...
        assert by_symbol["BTCUSDT"].ticker_status is DataStatus.OK
        assert by_symbol["BTCUSDT"].last_price is not None

        # ...while the quiet one is individually stale and carries no numbers.
        assert by_symbol["0GUSDT"].ticker_status is DataStatus.STALE
        assert by_symbol["0GUSDT"].last_price is None
        assert by_symbol["0GUSDT"].ticker_detail
    finally:
        await adapter.aclose()


async def test_whole_feed_stopped_is_still_reported_as_stale() -> None:
    """When even the newest ticker is old, that is a real outage."""
    service, adapter = build_service(handler(ticker_age=4000))
    try:
        page = await service.scan(ScanQuery())
        assert page.ticker_status is DataStatus.STALE
        assert all(r.last_price is None for r in page.rows)
    finally:
        await adapter.aclose()
