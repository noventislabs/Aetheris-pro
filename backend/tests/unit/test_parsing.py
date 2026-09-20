"""Binance response parsing: exact numbers, explicit absences, no repairs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.fixtures import binance_payloads as payloads

from aetheris.adapters.exchange.binance import parsing
from aetheris.adapters.exchange.errors import ExchangeInvalidResponseError
from aetheris.domain.enums import ContractType, SymbolStatus, Timeframe

# ----------------------------------------------------------------------
# Numeric conversion
# ----------------------------------------------------------------------


def test_string_numbers_convert_exactly() -> None:
    # 0.1 as a float would be 0.1000000000000000055...; as a string it is exact.
    assert parsing.decimal_from_json("0.1", field="x") == Decimal("0.1")


def test_json_float_is_routed_through_str_not_binary() -> None:
    assert parsing.decimal_from_json(0.1, field="x") == Decimal("0.1")


def test_missing_number_is_an_error_not_a_zero() -> None:
    with pytest.raises(ExchangeInvalidResponseError, match="missing numeric field"):
        parsing.decimal_from_json(None, field="lastPrice")


def test_boolean_is_rejected_as_a_number() -> None:
    with pytest.raises(ExchangeInvalidResponseError, match="boolean"):
        parsing.decimal_from_json(True, field="x")


def test_garbage_number_is_rejected() -> None:
    with pytest.raises(ExchangeInvalidResponseError, match="not a valid number"):
        parsing.decimal_from_json("about sixty thousand", field="x")


def test_optional_number_absent_stays_none() -> None:
    assert parsing.optional_decimal({}, "minNotional") is None
    assert parsing.optional_decimal({"minNotional": None}, "minNotional") is None


# ----------------------------------------------------------------------
# Symbols and filters
# ----------------------------------------------------------------------


def test_symbol_normalization_preserves_precision() -> None:
    symbol = parsing.parse_symbol(payloads.symbol_entry())
    assert symbol.symbol == "BTCUSDT"
    assert symbol.status is SymbolStatus.TRADING
    assert symbol.contract_type is ContractType.PERPETUAL
    assert symbol.filters.tick_size == Decimal("0.10")
    assert symbol.filters.step_size == Decimal("0.001")
    assert symbol.filters.min_notional == Decimal("100")


def test_leverage_is_none_because_public_data_cannot_supply_it() -> None:
    # Leverage brackets need an authenticated endpoint. Absent, not guessed.
    assert parsing.parse_symbol(payloads.symbol_entry()).max_leverage is None


def test_absent_min_notional_stays_none_rather_than_zero() -> None:
    symbol = parsing.parse_symbol(payloads.symbol_entry(min_notional=None))
    assert symbol.filters.min_notional is None


def test_absent_market_lot_filter_is_tolerated() -> None:
    symbol = parsing.parse_symbol(payloads.symbol_entry(include_market_lot=False))
    assert symbol.filters.market_step_size is None
    assert symbol.filters.step_size == Decimal("0.001")


def test_missing_required_filter_rejects_the_symbol() -> None:
    entry = payloads.symbol_entry()
    entry["filters"] = [f for f in entry["filters"] if f["filterType"] != "LOT_SIZE"]
    with pytest.raises(ExchangeInvalidResponseError, match="PRICE_FILTER or LOT_SIZE"):
        parsing.parse_symbol(entry)


def test_unknown_status_becomes_unknown_not_trading() -> None:
    # A lifecycle state Binance adds tomorrow must exclude the symbol, not be
    # silently treated as tradable.
    symbol = parsing.parse_symbol(payloads.symbol_entry(status="SOME_NEW_STATE"))
    assert symbol.status is SymbolStatus.UNKNOWN
    assert not parsing.is_eligible(symbol)


def test_missing_required_string_field_is_rejected() -> None:
    entry = payloads.symbol_entry()
    del entry["baseAsset"]
    with pytest.raises(ExchangeInvalidResponseError, match="baseAsset"):
        parsing.parse_symbol(entry)


# ----------------------------------------------------------------------
# Exchange info
# ----------------------------------------------------------------------


def test_exchange_info_parses_the_whole_universe() -> None:
    info = parsing.parse_exchange_info(payloads.exchange_info())
    assert info.symbol_count == 6
    assert info.server_time is not None


def test_one_bad_symbol_does_not_blind_the_scanner() -> None:
    bad = payloads.symbol_entry(symbol="BROKENUSDT")
    del bad["pricePrecision"]
    info = parsing.parse_exchange_info(payloads.exchange_info([payloads.symbol_entry(), bad]))
    assert [s.symbol for s in info.symbols] == ["BTCUSDT"]


def test_all_symbols_unparseable_is_a_contract_change_not_a_bad_row() -> None:
    bad = payloads.symbol_entry()
    del bad["pricePrecision"]
    with pytest.raises(ExchangeInvalidResponseError, match="no symbol"):
        parsing.parse_exchange_info(payloads.exchange_info([bad]))


def test_empty_symbol_list_is_valid_not_an_error() -> None:
    info = parsing.parse_exchange_info(payloads.exchange_info([]))
    assert info.symbol_count == 0


def test_non_object_response_is_rejected() -> None:
    with pytest.raises(ExchangeInvalidResponseError, match="not a JSON object"):
        parsing.parse_exchange_info(["unexpected"])


def test_missing_symbols_array_is_rejected() -> None:
    with pytest.raises(ExchangeInvalidResponseError, match="no symbols array"):
        parsing.parse_exchange_info({"serverTime": 1})


# ----------------------------------------------------------------------
# Eligibility
# ----------------------------------------------------------------------


def test_eligibility_is_a_pure_predicate_over_metadata() -> None:
    info = parsing.parse_exchange_info(payloads.exchange_info())
    eligible = {s.symbol for s in info.symbols if parsing.is_eligible(s)}
    # Discovered, not hardcoded: whatever the venue lists as a TRADING USDT-M
    # perpetual qualifies -- including a newly listed digit-leading ticker.
    assert eligible == {"BTCUSDT", "ETHUSDT", "0GUSDT"}


def test_ineligible_reasons_are_each_enforced() -> None:
    info = parsing.parse_exchange_info(payloads.exchange_info())
    by_symbol = {s.symbol: s for s in info.symbols}
    assert not parsing.is_eligible(by_symbol["BTCUSDC"])  # quote asset
    assert not parsing.is_eligible(by_symbol["BTCUSDT_250926"])  # not perpetual
    assert not parsing.is_eligible(by_symbol["HALTUSDT"])  # not trading


# ----------------------------------------------------------------------
# Ticker
# ----------------------------------------------------------------------


def test_ticker_normalization() -> None:
    ticker = parsing.parse_ticker(payloads.ticker_24h(), payloads.book_ticker())
    assert ticker.symbol == "BTCUSDT"
    assert ticker.last_price == Decimal("60050.10")
    assert ticker.bid_price == Decimal("60050.00")
    assert ticker.ask_price == Decimal("60050.20")
    assert ticker.high_24h == Decimal("60500.00")
    assert ticker.event_time is not None


def test_ticker_without_book_has_no_bid_or_ask() -> None:
    ticker = parsing.parse_ticker(payloads.ticker_24h(), None)
    assert ticker.bid_price is None
    assert ticker.ask_price is None
    assert ticker.last_price == Decimal("60050.10")


def test_ticker_missing_last_price_is_rejected() -> None:
    raw = payloads.ticker_24h()
    del raw["lastPrice"]
    with pytest.raises(ExchangeInvalidResponseError, match="lastPrice"):
        parsing.parse_ticker(raw)


# ----------------------------------------------------------------------
# Candles
# ----------------------------------------------------------------------


def test_klines_normalization() -> None:
    series = parsing.parse_klines(
        payloads.klines(count=4), symbol="BTCUSDT", timeframe=Timeframe.H1
    )
    assert len(series.candles) == 4
    assert series.candles[0].open == Decimal("60000.0")
    assert series.candles[0].trade_count == 120
    assert series.last_close_time is not None


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("high", "59000.0", "high below low"),
        ("open", "70000.0", "open above high"),
        ("close", "10.0", "close below low"),
        ("volume", "-5", "negative volume"),
    ],
)
def test_corrupt_candles_are_rejected_not_repaired(field: str, value: str, reason: str) -> None:
    row = payloads.kline_row(open_time=payloads.now(), interval_seconds=3600)
    index = {"open": 1, "high": 2, "low": 3, "close": 4, "volume": 5}[field]
    row[index] = value
    with pytest.raises(ExchangeInvalidResponseError, match="failed candle validation"):
        parsing.parse_candle(row)


def test_inverted_times_are_rejected() -> None:
    row = payloads.kline_row(open_time=payloads.now(), interval_seconds=3600)
    row[6] = row[0] - 1000  # close before open
    with pytest.raises(ExchangeInvalidResponseError, match="failed candle validation"):
        parsing.parse_candle(row)


def test_short_kline_row_is_rejected() -> None:
    with pytest.raises(ExchangeInvalidResponseError, match="expected length"):
        parsing.parse_candle([1, "2", "3"])


def test_malformed_kline_number_is_rejected() -> None:
    row = payloads.kline_row(open_time=payloads.now(), interval_seconds=3600)
    row[1] = "not-a-price"
    with pytest.raises(ExchangeInvalidResponseError, match="not a valid number"):
        parsing.parse_candle(row)


def test_non_array_klines_response_is_rejected() -> None:
    with pytest.raises(ExchangeInvalidResponseError, match="not a JSON array"):
        parsing.parse_klines({"code": -1121}, symbol="BTCUSDT", timeframe=Timeframe.H1)


def test_out_of_order_candles_are_rejected() -> None:
    base = payloads.now()
    rows = [
        payloads.kline_row(open_time=base, interval_seconds=3600),
        payloads.kline_row(open_time=base - timedelta(hours=1), interval_seconds=3600),
    ]
    with pytest.raises(ExchangeInvalidResponseError, match="ordering validation"):
        parsing.parse_klines(rows, symbol="BTCUSDT", timeframe=Timeframe.H1)


def test_timestamp_conversion_is_utc_aware() -> None:
    moment = parsing.timestamp_from_millis(1_700_000_000_000, field="t")
    assert moment.tzinfo is not None
    assert moment == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_non_integer_timestamp_is_rejected() -> None:
    with pytest.raises(ExchangeInvalidResponseError, match="epoch-millisecond"):
        parsing.timestamp_from_millis("1700000000000", field="t")
