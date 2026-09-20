"""Normalized market-data models.

These are Aetheris Pro's own shapes, not any venue's. A venue response is
translated into them at the adapter boundary, so the scanner, strategy engine
and risk engine never learn what an exchange's JSON looks like -- and a second
exchange can be added without touching anything above the adapter.

Every monetary field is ``Decimal``. Fields the venue may omit are explicitly
optional: absent is represented as ``None``, never as zero.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aetheris.domain.enums import ConnectionStatus, ContractType, SymbolStatus, Timeframe


class SymbolFilters(BaseModel):
    """Venue constraints an order must satisfy.

    Carried on every symbol because the risk engine (phase 7) must be able to
    reject an order for precision or notional reasons *before* it reaches the
    venue, and the backtester must simulate the same constraints.
    """

    model_config = ConfigDict(frozen=True)

    tick_size: Decimal = Field(gt=0, description="Price increment")
    step_size: Decimal = Field(gt=0, description="Quantity increment")
    min_quantity: Decimal = Field(ge=0)
    max_quantity: Decimal | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    min_notional: Decimal | None = Field(
        default=None, description="None when the venue does not publish one"
    )
    market_step_size: Decimal | None = None
    market_min_quantity: Decimal | None = None
    market_max_quantity: Decimal | None = None


class Symbol(BaseModel):
    """A tradable instrument, normalized."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    base_asset: str
    quote_asset: str
    margin_asset: str | None = None
    status: SymbolStatus
    contract_type: ContractType
    price_precision: int = Field(ge=0)
    quantity_precision: int = Field(ge=0)
    base_asset_precision: int | None = Field(default=None, ge=0)
    quote_precision: int | None = Field(default=None, ge=0)
    filters: SymbolFilters
    onboard_date: datetime | None = None
    delivery_date: datetime | None = None
    order_types: tuple[str, ...] = ()
    time_in_force: tuple[str, ...] = ()
    #: Leverage brackets are served only by an authenticated endpoint, so this
    #: stays None for public market data rather than being guessed.
    max_leverage: int | None = None


class ExchangeInfo(BaseModel):
    """Venue metadata snapshot."""

    model_config = ConfigDict(frozen=True)

    exchange: str
    server_time: datetime | None = None
    symbols: tuple[Symbol, ...]

    @property
    def symbol_count(self) -> int:
        return len(self.symbols)


class Ticker(BaseModel):
    """24-hour rolling statistics plus best bid/ask where published."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    last_price: Decimal
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    high_24h: Decimal | None = None
    low_24h: Decimal | None = None
    open_24h: Decimal | None = None
    volume_24h: Decimal | None = None
    quote_volume_24h: Decimal | None = None
    price_change_24h: Decimal | None = None
    price_change_percent_24h: Decimal | None = None
    event_time: datetime | None = None


class Candle(BaseModel):
    """A single OHLCV bar.

    The validator enforces internal consistency. A bar that fails it is not
    repaired and not partially accepted -- the adapter turns the failure into
    an ``EXCHANGE_INVALID_RESPONSE``. Quietly clamping a high below its low
    would put fabricated prices into a backtest.
    """

    model_config = ConfigDict(frozen=True)

    open_time: datetime
    close_time: datetime
    open: Decimal = Field(ge=0)
    high: Decimal = Field(ge=0)
    low: Decimal = Field(ge=0)
    close: Decimal = Field(ge=0)
    volume: Decimal = Field(ge=0)
    quote_volume: Decimal | None = Field(default=None, ge=0)
    trade_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.high < self.low:
            raise ValueError(f"candle high {self.high} is below low {self.low}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"candle open {self.open} outside [{self.low}, {self.high}]")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"candle close {self.close} outside [{self.low}, {self.high}]")
        if self.close_time <= self.open_time:
            raise ValueError("candle close_time must be after open_time")
        return self


class CandleSeries(BaseModel):
    """An ordered run of candles for one symbol and timeframe."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: Timeframe
    candles: tuple[Candle, ...]

    @model_validator(mode="after")
    def _check_ordering(self) -> Self:
        """Reject out-of-order or duplicated bars.

        A series that is not strictly increasing in time silently breaks every
        indicator computed from it, so it is refused at the boundary rather
        than debugged later from inside a strategy.
        """
        previous: datetime | None = None
        for candle in self.candles:
            if previous is not None and candle.open_time <= previous:
                raise ValueError("candles must be strictly ordered by open_time")
            previous = candle.open_time
        return self

    @property
    def last_close_time(self) -> datetime | None:
        return self.candles[-1].close_time if self.candles else None


class MarketDataStatus(BaseModel):
    """Truthful report of the market-data connection.

    ``connection_status`` is derived from observed outcomes only. With no
    successful response yet it is UNKNOWN -- having configuration never counts
    as being connected.
    """

    model_config = ConfigDict(frozen=True)

    exchange: str
    source: str
    connection_status: ConnectionStatus
    last_success: datetime | None = None
    last_failure: datetime | None = None
    last_error_code: str | None = None
    last_success_age_seconds: float | None = None
    symbols_discovered: int | None = None
    eligible_symbols: int | None = None
    universe_updated_at: datetime | None = None
