"""Core domain enumerations.

These names are the stable vocabulary of the system: they cross the API
boundary, land in the database, and appear in audit logs. Treat changes here as
schema changes.
"""

from __future__ import annotations

from enum import StrEnum


class TradingMode(StrEnum):
    """Execution modes. No mode may automatically escalate into the next."""

    ANALYSIS = "ANALYSIS"
    PAPER = "PAPER"
    TESTNET = "TESTNET"
    LIVE = "LIVE"

    @property
    def places_real_orders(self) -> bool:
        """True when the mode reaches a real exchange matching engine.

        TESTNET counts: it is a real exchange endpoint with real order records,
        just settled in worthless funds.
        """
        return self in (TradingMode.TESTNET, TradingMode.LIVE)

    @property
    def risks_real_funds(self) -> bool:
        return self is TradingMode.LIVE


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> OrderSide:
        return OrderSide.SELL if self is OrderSide.BUY else OrderSide.BUY


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT = "TAKE_PROFIT"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderState(StrEnum):
    """Order lifecycle states.

    UNKNOWN and RECONCILING are not error states -- they are the honest
    representation of "the exchange has not told us yet". The order engine must
    never collapse them into FILLED or CANCELLED by assumption.
    """

    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_ORDER_STATES

    @property
    def is_open(self) -> bool:
        """Open orders may still consume margin or produce fills."""
        return self in _OPEN_ORDER_STATES


_TERMINAL_ORDER_STATES = frozenset(
    {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED}
)

_OPEN_ORDER_STATES = frozenset(
    {
        OrderState.SUBMITTED,
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCEL_REQUESTED,
    }
)


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def seconds(self) -> int:
        return _TIMEFRAME_SECONDS[self]


_TIMEFRAME_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.H1: 3_600,
    Timeframe.H4: 14_400,
    Timeframe.D1: 86_400,
}
