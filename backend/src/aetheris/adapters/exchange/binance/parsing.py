"""Translation from Binance JSON into Aetheris domain models.

This module is the only place that knows Binance's field names and array
layouts. Everything it returns is a normalized domain object, so nothing above
the adapter can accidentally depend on a venue's wire format.

Two rules govern every function here:

* **Missing or unparseable required data is an error, not a default.** A price
  that will not parse never becomes zero.
* **Optional data that is absent stays absent.** ``None`` means the venue did
  not publish it, which is different from publishing zero.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from pydantic import ValidationError

from aetheris.adapters.exchange.errors import ExchangeInvalidResponseError
from aetheris.core.logging import get_logger
from aetheris.core.money import InvalidMoneyError, to_decimal
from aetheris.domain.enums import ContractType, SymbolStatus, Timeframe
from aetheris.domain.market import (
    Candle,
    CandleSeries,
    ExchangeInfo,
    Symbol,
    SymbolFilters,
    Ticker,
)

_log = get_logger("exchange.binance.parsing")

#: Binance kline rows are positional arrays. Naming the indices keeps the
#: parser readable and makes an arity change fail loudly instead of silently
#: shifting every field by one.
_K_OPEN_TIME: Final = 0
_K_OPEN: Final = 1
_K_HIGH: Final = 2
_K_LOW: Final = 3
_K_CLOSE: Final = 4
_K_VOLUME: Final = 5
_K_CLOSE_TIME: Final = 6
_K_QUOTE_VOLUME: Final = 7
_K_TRADE_COUNT: Final = 8
_KLINE_MIN_FIELDS: Final = 9


def _fail(message: str, **details: Any) -> ExchangeInvalidResponseError:
    """Build the standard invalid-response error.

    ``message`` is positional so that ``**details`` can carry any field
    name -- including ``reason`` -- without colliding with it.
    """
    return ExchangeInvalidResponseError(message, details=details or None)


def decimal_from_json(value: Any, *, field: str) -> Decimal:
    """Convert a JSON scalar to ``Decimal`` without losing precision.

    Binance sends numbers as strings, which convert exactly. A JSON ``float``
    would already have lost precision before we saw it, so it is routed through
    ``str()`` -- the acknowledged, explicit conversion that
    :mod:`aetheris.core.money` requires rather than forbidding outright, since
    refusing it here would mean discarding an otherwise valid response.
    """
    if value is None:
        raise _fail(f"missing numeric field {field!r}", field=field)
    if isinstance(value, bool):
        raise _fail(f"field {field!r} is a boolean, not a number", field=field)
    try:
        if isinstance(value, float):
            return to_decimal(str(value))
        if isinstance(value, Decimal | int | str):
            return to_decimal(value)
    except InvalidMoneyError as exc:
        raise _fail(f"field {field!r} is not a valid number", field=field) from exc
    raise _fail(f"field {field!r} has unexpected type {type(value).__name__}", field=field)


def optional_decimal(raw: dict[str, Any], key: str) -> Decimal | None:
    """Parse an optional numeric field, preserving 'not published' as ``None``."""
    if raw.get(key) is None:
        return None
    return decimal_from_json(raw[key], field=key)


def timestamp_from_millis(value: Any, *, field: str) -> datetime:
    """Convert epoch milliseconds to an aware UTC datetime."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise _fail(f"field {field!r} is not an epoch-millisecond integer", field=field)
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise _fail(f"field {field!r} is not a representable timestamp", field=field) from exc


def optional_timestamp(raw: dict[str, Any], key: str) -> datetime | None:
    """Parse an optional timestamp. Binance writes 'absent' as 0."""
    value = raw.get(key)
    if value is None or value == 0:
        return None
    try:
        return timestamp_from_millis(value, field=key)
    except ExchangeInvalidResponseError:
        # An unrepresentable optional date is dropped, not fatal: perpetual
        # contracts carry sentinel delivery dates far in the future.
        return None


def _extract_filters(raw_filters: Any, *, symbol: str) -> SymbolFilters:
    """Collapse Binance's list-of-filter-objects into one typed record."""
    if not isinstance(raw_filters, list):
        raise _fail(f"symbol {symbol} has no filter list", symbol=symbol)

    by_type: dict[str, dict[str, Any]] = {}
    for entry in raw_filters:
        if isinstance(entry, dict) and isinstance(entry.get("filterType"), str):
            by_type[entry["filterType"]] = entry

    price = by_type.get("PRICE_FILTER")
    lot = by_type.get("LOT_SIZE")
    if price is None or lot is None:
        raise _fail(
            f"symbol {symbol} is missing PRICE_FILTER or LOT_SIZE",
            symbol=symbol,
            filters_present=sorted(by_type),
        )

    market_lot = by_type.get("MARKET_LOT_SIZE", {})
    notional = by_type.get("MIN_NOTIONAL", {})

    return SymbolFilters(
        tick_size=decimal_from_json(price.get("tickSize"), field="tickSize"),
        step_size=decimal_from_json(lot.get("stepSize"), field="stepSize"),
        min_quantity=decimal_from_json(lot.get("minQty"), field="minQty"),
        max_quantity=optional_decimal(lot, "maxQty"),
        min_price=optional_decimal(price, "minPrice"),
        max_price=optional_decimal(price, "maxPrice"),
        # Futures publishes the minimum notional under "notional"; spot uses
        # "minNotional". Accept either, and stay None when neither is present.
        min_notional=optional_decimal(notional, "notional")
        or optional_decimal(notional, "minNotional"),
        market_step_size=optional_decimal(market_lot, "stepSize"),
        market_min_quantity=optional_decimal(market_lot, "minQty"),
        market_max_quantity=optional_decimal(market_lot, "maxQty"),
    )


def _enum_or_unknown[E: (SymbolStatus, ContractType)](
    enum_cls: type[E], value: Any, unknown: E
) -> E:
    """Map a venue string onto our vocabulary, preserving unrecognised values.

    A status we have never seen becomes UNKNOWN rather than raising: a new
    Binance lifecycle state should exclude a symbol from trading, not take the
    whole universe down.
    """
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            return unknown
    return unknown


def _require_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise _fail(f"missing required string field {key!r}", field=key)
    return value


def _require_int(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _fail(f"missing required integer field {key!r}", field=key)
    return value


def _optional_int(raw: dict[str, Any], key: str) -> int | None:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def _string_tuple(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def parse_symbol(raw: dict[str, Any]) -> Symbol:
    """Normalize one exchangeInfo symbol entry."""
    name = _require_str(raw, "symbol")
    return Symbol(
        symbol=name,
        base_asset=_require_str(raw, "baseAsset"),
        quote_asset=_require_str(raw, "quoteAsset"),
        margin_asset=raw.get("marginAsset") if isinstance(raw.get("marginAsset"), str) else None,
        status=_enum_or_unknown(SymbolStatus, raw.get("status"), SymbolStatus.UNKNOWN),
        contract_type=_enum_or_unknown(ContractType, raw.get("contractType"), ContractType.UNKNOWN),
        price_precision=_require_int(raw, "pricePrecision"),
        quantity_precision=_require_int(raw, "quantityPrecision"),
        base_asset_precision=_optional_int(raw, "baseAssetPrecision"),
        quote_precision=_optional_int(raw, "quotePrecision"),
        filters=_extract_filters(raw.get("filters"), symbol=name),
        onboard_date=optional_timestamp(raw, "onboardDate"),
        delivery_date=optional_timestamp(raw, "deliveryDate"),
        order_types=_string_tuple(raw, "orderTypes"),
        time_in_force=_string_tuple(raw, "timeInForce"),
        # Leverage brackets require an authenticated endpoint; public data
        # cannot supply this, so it stays unknown rather than being invented.
        max_leverage=None,
    )


def parse_exchange_info(raw: Any) -> ExchangeInfo:
    """Normalize a full exchangeInfo document.

    A symbol that cannot be parsed is skipped with a logged reason rather than
    failing the whole universe -- one malformed instrument should not blind the
    scanner to the other four hundred. If *every* symbol fails, that is a
    contract change rather than a bad row, and the response is rejected.
    """
    if not isinstance(raw, dict):
        raise _fail("exchangeInfo response was not a JSON object")

    raw_symbols = raw.get("symbols")
    if not isinstance(raw_symbols, list):
        raise _fail("exchangeInfo response has no symbols array")

    symbols: list[Symbol] = []
    skipped = 0
    for entry in raw_symbols:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        try:
            symbols.append(parse_symbol(entry))
        except (ExchangeInvalidResponseError, ValidationError) as exc:
            skipped += 1
            _log.warning(
                "exchange_symbol_skipped",
                symbol=entry.get("symbol"),
                reason=str(exc)[:200],
            )

    if raw_symbols and not symbols:
        raise _fail(
            "no symbol in the exchangeInfo response could be parsed",
            symbols_received=len(raw_symbols),
        )
    if skipped:
        _log.info("exchange_symbols_partial", parsed=len(symbols), skipped=skipped)

    server_time = raw.get("serverTime")
    return ExchangeInfo(
        exchange="binance-futures-usdm",
        server_time=(
            timestamp_from_millis(server_time, field="serverTime")
            if isinstance(server_time, int) and not isinstance(server_time, bool)
            else None
        ),
        symbols=tuple(symbols),
    )


def is_eligible(symbol: Symbol, *, quote_asset: str = "USDT") -> bool:
    """Deterministic eligibility test for the scanner universe.

    Intentionally a pure predicate over normalized metadata, with no symbol
    names in it: newly listed perpetuals qualify automatically the moment the
    venue lists them, and delisted ones drop out on their own.
    """
    return (
        symbol.contract_type is ContractType.PERPETUAL
        and symbol.quote_asset == quote_asset
        and symbol.status is SymbolStatus.TRADING
        and symbol.filters.tick_size > 0
        and symbol.filters.step_size > 0
    )


def parse_ticker(raw: Any, book: Any = None) -> Ticker:
    """Normalize a 24hr ticker, merging best bid/ask when available.

    ``book`` comes from a separate endpoint. When it is missing or unusable,
    bid and ask stay ``None`` -- the ticker is still truthful, just without
    top-of-book.
    """
    if not isinstance(raw, dict):
        raise _fail("ticker response was not a JSON object")

    symbol = _require_str(raw, "symbol")
    bid: Decimal | None = None
    ask: Decimal | None = None
    if isinstance(book, dict):
        bid = optional_decimal(book, "bidPrice")
        ask = optional_decimal(book, "askPrice")

    return Ticker(
        symbol=symbol,
        last_price=decimal_from_json(raw.get("lastPrice"), field="lastPrice"),
        bid_price=bid,
        ask_price=ask,
        high_24h=optional_decimal(raw, "highPrice"),
        low_24h=optional_decimal(raw, "lowPrice"),
        open_24h=optional_decimal(raw, "openPrice"),
        volume_24h=optional_decimal(raw, "volume"),
        quote_volume_24h=optional_decimal(raw, "quoteVolume"),
        price_change_24h=optional_decimal(raw, "priceChange"),
        price_change_percent_24h=optional_decimal(raw, "priceChangePercent"),
        event_time=optional_timestamp(raw, "closeTime") or optional_timestamp(raw, "time"),
    )


def parse_candle(row: Any) -> Candle:
    """Normalize one kline row, validating it rather than repairing it."""
    if not isinstance(row, list) or len(row) < _KLINE_MIN_FIELDS:
        raise _fail("kline row is not an array of the expected length")

    try:
        return Candle(
            open_time=timestamp_from_millis(row[_K_OPEN_TIME], field="openTime"),
            close_time=timestamp_from_millis(row[_K_CLOSE_TIME], field="closeTime"),
            open=decimal_from_json(row[_K_OPEN], field="open"),
            high=decimal_from_json(row[_K_HIGH], field="high"),
            low=decimal_from_json(row[_K_LOW], field="low"),
            close=decimal_from_json(row[_K_CLOSE], field="close"),
            volume=decimal_from_json(row[_K_VOLUME], field="volume"),
            quote_volume=decimal_from_json(row[_K_QUOTE_VOLUME], field="quoteVolume"),
            trade_count=(
                row[_K_TRADE_COUNT]
                if isinstance(row[_K_TRADE_COUNT], int)
                and not isinstance(row[_K_TRADE_COUNT], bool)
                else None
            ),
        )
    except ValidationError as exc:
        # Candle.model_validator rejected it: high below low, open outside the
        # range, negative volume, inverted times. Not repairable.
        raise _fail("kline row failed candle validation", reason=str(exc)[:200]) from exc


def parse_klines(raw: Any, *, symbol: str, timeframe: Timeframe) -> CandleSeries:
    """Normalize a full kline response into an ordered series."""
    if not isinstance(raw, list):
        raise _fail("klines response was not a JSON array", symbol=symbol)

    candles = tuple(parse_candle(row) for row in raw)
    try:
        return CandleSeries(symbol=symbol, timeframe=timeframe, candles=candles)
    except ValidationError as exc:
        raise _fail("kline series failed ordering validation", reason=str(exc)[:200]) from exc
