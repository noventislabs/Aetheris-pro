"""Realistic Binance USDT-M Futures payloads for tests.

Shapes follow the public API documentation: numbers arrive as strings,
timestamps as epoch milliseconds, klines as positional arrays.

Timestamps are built relative to "now" so freshness assertions stay meaningful
whenever the suite runs, rather than going stale on a fixed date.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any


def millis(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def now() -> datetime:
    return datetime.now(UTC)


def symbol_entry(
    *,
    symbol: str = "BTCUSDT",
    base_asset: str = "BTC",
    quote_asset: str = "USDT",
    status: str = "TRADING",
    contract_type: str = "PERPETUAL",
    tick_size: str = "0.10",
    step_size: str = "0.001",
    min_qty: str = "0.001",
    min_notional: str | None = "100",
    include_market_lot: bool = True,
) -> dict[str, Any]:
    """One exchangeInfo symbol entry."""
    filters: list[dict[str, Any]] = [
        {
            "filterType": "PRICE_FILTER",
            "minPrice": "556.80",
            "maxPrice": "4529764",
            "tickSize": tick_size,
        },
        {
            "filterType": "LOT_SIZE",
            "maxQty": "1000",
            "minQty": min_qty,
            "stepSize": step_size,
        },
    ]
    if include_market_lot:
        filters.append(
            {
                "filterType": "MARKET_LOT_SIZE",
                "maxQty": "120",
                "minQty": min_qty,
                "stepSize": step_size,
            }
        )
    if min_notional is not None:
        filters.append({"filterType": "MIN_NOTIONAL", "notional": min_notional})

    return {
        "symbol": symbol,
        "pair": symbol,
        "contractType": contract_type,
        "deliveryDate": 4133404800000,
        "onboardDate": millis(now() - timedelta(days=400)),
        "status": status,
        "baseAsset": base_asset,
        "quoteAsset": quote_asset,
        "marginAsset": "USDT",
        "pricePrecision": 2,
        "quantityPrecision": 3,
        "baseAssetPrecision": 8,
        "quotePrecision": 8,
        "orderTypes": ["LIMIT", "MARKET", "STOP_MARKET"],
        "timeInForce": ["GTC", "IOC", "FOK", "GTX"],
        "filters": filters,
    }


def exchange_info(symbols: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A full exchangeInfo document.

    The default universe deliberately mixes eligible and ineligible
    instruments: a quarterly future, a non-USDT pair and a halted symbol, so
    eligibility filtering is exercised rather than assumed. ``0GUSDT`` stands
    in for a newly listed contract with a digit-leading ticker.
    """
    if symbols is None:
        symbols = [
            symbol_entry(),
            symbol_entry(symbol="ETHUSDT", base_asset="ETH", tick_size="0.01"),
            symbol_entry(symbol="0GUSDT", base_asset="0G", tick_size="0.0001"),
            symbol_entry(
                symbol="BTCUSDC", base_asset="BTC", quote_asset="USDC"
            ),  # wrong quote asset
            symbol_entry(symbol="BTCUSDT_250926", contract_type="CURRENT_QUARTER"),  # not perpetual
            symbol_entry(symbol="HALTUSDT", base_asset="HALT", status="HALT"),  # not trading
        ]
    return {
        "timezone": "UTC",
        "serverTime": millis(now()),
        "rateLimits": [],
        "exchangeFilters": [],
        "assets": [],
        "symbols": symbols,
    }


def ticker_24h(
    *,
    symbol: str = "BTCUSDT",
    last_price: str = "60050.10",
    age_seconds: float = 1.0,
) -> dict[str, Any]:
    close_time = now() - timedelta(seconds=age_seconds)
    return {
        "symbol": symbol,
        "priceChange": "150.30",
        "priceChangePercent": "0.251",
        "weightedAvgPrice": "59980.55",
        "lastPrice": last_price,
        "lastQty": "0.015",
        "openPrice": "59899.80",
        "highPrice": "60500.00",
        "lowPrice": "59500.00",
        "volume": "125430.221",
        "quoteVolume": "7523456789.10",
        "openTime": millis(now() - timedelta(hours=24)),
        "closeTime": millis(close_time),
        "firstId": 1,
        "lastId": 2,
        "count": 3,
    }


def book_ticker(
    *, symbol: str = "BTCUSDT", bid: str = "60050.00", ask: str = "60050.20"
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "bidPrice": bid,
        "bidQty": "3.512",
        "askPrice": ask,
        "askQty": "2.104",
        "time": millis(now()),
        "lastUpdateId": 99,
    }


def kline_row(
    *,
    open_time: datetime,
    interval_seconds: int,
    open_: str = "60000.0",
    high: str = "60100.0",
    low: str = "59900.0",
    close: str = "60050.0",
    volume: str = "12.5",
) -> list[Any]:
    close_time = open_time + timedelta(seconds=interval_seconds)
    return [
        millis(open_time),
        open_,
        high,
        low,
        close,
        volume,
        millis(close_time) - 1,
        "750000.0",
        120,
        "6.0",
        "360000.0",
        "0",
    ]


def klines(*, count: int = 5, interval_seconds: int = 3600) -> list[list[Any]]:
    """A run of candles ending with one that is still forming."""
    start = now() - timedelta(seconds=interval_seconds * (count - 1))
    return [
        kline_row(
            open_time=start + timedelta(seconds=interval_seconds * index),
            interval_seconds=interval_seconds,
        )
        for index in range(count)
    ]


def trending_klines(
    *, count: int = 220, interval_seconds: int = 3600, drift: str = "0.6"
) -> list[list[Any]]:
    """A rising series with regular pullbacks, ending with a forming bar.

    ``klines()`` emits identical bars, which is right for testing transport
    and parsing but cannot produce a directional signal: with no directional
    movement ADX never warms up, so the strategy correctly reports
    INSUFFICIENT_DATA. A route test that only ever saw that shape would never
    exercise the scored path at all.

    Drift plus a sine pullback is the smallest shape that yields a real
    LONG bias -- a pure ramp pins RSI above the overbought band, which the
    rule set also correctly refuses. Deterministic, so the test is too.
    """
    start = now() - timedelta(seconds=interval_seconds * (count - 1))
    step = Decimal(drift)
    rows: list[list[Any]] = []
    for index in range(count):
        wave = Decimal(str(round(10 * math.sin(2 * math.pi * index / 12), 4)))
        close = Decimal(200) + Decimal(index) * step + wave
        rows.append(
            kline_row(
                open_time=start + timedelta(seconds=interval_seconds * index),
                interval_seconds=interval_seconds,
                open_=f"{close:.4f}",
                high=f"{close + Decimal('1.5'):.4f}",
                low=f"{close - Decimal('1.5'):.4f}",
                close=f"{close:.4f}",
            )
        )
    return rows


def stale_klines(*, count: int = 3, interval_seconds: int = 3600) -> list[list[Any]]:
    """Candles whose most recent bar closed long ago."""
    start = now() - timedelta(seconds=interval_seconds * 50)
    return [
        kline_row(
            open_time=start + timedelta(seconds=interval_seconds * index),
            interval_seconds=interval_seconds,
        )
        for index in range(count)
    ]


def ticker_24h_list(
    symbols: list[str],
    *,
    age_seconds: float = 1.0,
    prices: dict[str, str] | None = None,
    quote_volumes: dict[str, str] | None = None,
    change_percents: dict[str, str] | None = None,
    ages: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Whole-market 24h snapshot, as Binance serves it with no symbol filter.

    ``ages`` overrides the age of individual entries, which is how a real
    snapshot looks: heavily traded contracts are seconds old while quiet ones
    are minutes behind.
    """
    prices = prices or {}
    quote_volumes = quote_volumes or {}
    change_percents = change_percents or {}
    ages = ages or {}
    entries = []
    for symbol in symbols:
        entry = ticker_24h(
            symbol=symbol,
            last_price=prices.get(symbol, "100.00"),
            age_seconds=ages.get(symbol, age_seconds),
        )
        entry["quoteVolume"] = quote_volumes.get(symbol, "1000000.00")
        entry["priceChangePercent"] = change_percents.get(symbol, "1.000")
        entries.append(entry)
    return entries


def book_ticker_list(symbols: list[str]) -> list[dict[str, Any]]:
    return [book_ticker(symbol=symbol) for symbol in symbols]
