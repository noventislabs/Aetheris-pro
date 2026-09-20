"""Binance USDT-M Futures public REST paths.

Collected in one place so the base URL and paths are configuration and
constants, never string literals scattered through call sites. Every path here
is public: none requires an API key, and none can place an order.
"""

from __future__ import annotations

from typing import Final

#: Venue identity, used for provenance labels and status reporting.
EXCHANGE_NAME: Final = "binance-futures-usdm"
SOURCE_REST: Final = f"{EXCHANGE_NAME}:rest"

PING: Final = "/fapi/v1/ping"
SERVER_TIME: Final = "/fapi/v1/time"
EXCHANGE_INFO: Final = "/fapi/v1/exchangeInfo"
TICKER_24H: Final = "/fapi/v1/ticker/24hr"
BOOK_TICKER: Final = "/fapi/v1/ticker/bookTicker"
KLINES: Final = "/fapi/v1/klines"

#: Binance refuses a /klines request above this, so the API bounds to it rather
#: than letting the venue reject a request we could have rejected ourselves.
MAX_KLINES_LIMIT: Final = 1500
