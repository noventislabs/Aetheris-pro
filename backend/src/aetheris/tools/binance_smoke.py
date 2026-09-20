"""Opt-in live smoke test against Binance public market data.

Run manually::

    python -m aetheris.tools.binance_smoke

It is deliberately a script rather than a test: CI must never depend on a third
party being reachable, and a red build caused by someone else's outage teaches
a team to ignore red builds.

It reads public endpoints only -- no credentials, no orders. If Binance is
unreachable it says so and exits non-zero rather than inventing a result.
"""

from __future__ import annotations

import asyncio
import sys

from aetheris.adapters.exchange.binance.adapter import BinanceFuturesMarketDataAdapter
from aetheris.adapters.exchange.errors import ExchangeError
from aetheris.core.config import get_settings
from aetheris.core.logging import configure_logging
from aetheris.domain.enums import Timeframe


async def _run() -> int:
    settings = get_settings()
    adapter = BinanceFuturesMarketDataAdapter(
        settings=settings.binance, market_data=settings.market_data
    )

    try:
        print(f"base url        : {settings.binance.futures_rest_base_url}")
        info = await adapter.get_exchange_info()
        eligible = await adapter.get_symbols(eligible_only=True)
        print(f"server time     : {info.server_time}")
        print(f"symbols listed  : {info.symbol_count}")
        print(f"eligible (USDT-M perpetual, TRADING): {len(eligible)}")

        if not eligible:
            print("no eligible symbols returned; nothing further to sample")
            return 1

        # Sample whatever the venue actually listed first -- no hardcoded symbol.
        sample = eligible[0].symbol
        print(f"sample symbol   : {sample}")

        ticker = await adapter.get_ticker(sample)
        print(f"ticker status   : {ticker.status}")
        print(f"ticker source   : {ticker.source}")
        print(f"ticker age (s)  : {ticker.age_seconds}")
        if ticker.value is not None:
            print(f"last price      : {ticker.value.last_price}")
            print(f"bid / ask       : {ticker.value.bid_price} / {ticker.value.ask_price}")
        else:
            print(f"ticker detail   : {ticker.detail}")

        klines = await adapter.get_klines(sample, Timeframe.H1, limit=5)
        print(f"klines status   : {klines.status}")
        if klines.value is not None:
            print(f"candles         : {len(klines.value.candles)}")
            last = klines.value.candles[-1]
            print(f"last candle     : {last.open_time} O={last.open} C={last.close}")
        else:
            print(f"klines detail   : {klines.detail}")

        status = await adapter.get_market_status()
        print(f"connection      : {status.connection_status}")
        return 0

    except ExchangeError as exc:
        print(f"BINANCE_UNAVAILABLE: {exc.code} - {exc.message}", file=sys.stderr)
        return 2
    finally:
        await adapter.aclose()


def main() -> int:
    configure_logging(debug=True, level="INFO")
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
