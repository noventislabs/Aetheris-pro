"""Venue account state, expressed without naming a venue.

These types exist so the execution port can talk about leverage ceilings,
margin mode and position mode without the pure layers learning what an exchange
is called. Every field is something a venue *told us*; nothing here has a
default that would let an unknown become an assumption.

The recurring rule, applied again: a ceiling that could not be read is ``None``
and refuses the order. It is never the number we hoped for.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "LeverageBracket",
    "MarginMode",
    "PositionMode",
    "VenueAccount",
    "VenuePosition",
]


class PositionMode(StrEnum):
    """How the venue accounts for opposing positions in one symbol."""

    #: One net position per symbol. The only mode this build supports.
    ONE_WAY = "ONE_WAY"
    #: Independent long and short positions. Every order must then name which
    #: side it belongs to, and a close must target the right one. Supporting it
    #: half-way is worse than refusing it, so this build refuses it.
    HEDGE = "HEDGE"


class MarginMode(StrEnum):
    """Whether a position's margin is ring-fenced or drawn from the account."""

    #: Loss is bounded by the margin committed to that position.
    ISOLATED = "ISOLATED"
    #: Loss can draw on the whole balance.
    CROSSED = "CROSSED"


class VenueAccount(BaseModel):
    """Who we are at the venue, and what it will let us do."""

    model_config = ConfigDict(frozen=True)

    #: The venue's own identifier for the account. Recorded so order records
    #: cannot be silently attributed to a different account after a key swap.
    account_id: str
    position_mode: PositionMode
    #: ``None`` means the venue did not say, which is **not** the same as a
    #: refusal and must never be rendered as one. Callers treat anything other
    #: than ``True`` as "do not trade", so unknown still fails closed -- the
    #: distinction exists so an operator reading a refusal can tell "the venue
    #: disabled this key" from "we could not find out".
    can_trade: bool | None
    #: Available balance, when the venue reports one. ``None`` means unread,
    #: not zero.
    available_balance: Decimal | None = None


class LeverageBracket(BaseModel):
    """The venue's leverage ceiling for one symbol at one notional tier.

    Tiered on purpose, and the tier matters: a venue that allows 125x on a
    small position allows far less on a large one. Selecting the first bracket
    rather than the one containing the intended notional silently over-permits
    exactly the orders that are big enough to hurt.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    max_leverage: Decimal = Field(gt=0)
    notional_floor: Decimal = Field(ge=0)
    #: ``None`` where the venue leaves the top tier open-ended.
    notional_cap: Decimal | None = None
    maint_margin_ratio: Decimal | None = None

    def covers(self, notional: Decimal) -> bool:
        if notional < self.notional_floor:
            return False
        return self.notional_cap is None or notional <= self.notional_cap


class VenuePosition(BaseModel):
    """One open position, as the venue reports it.

    Every field is something the venue said. ``None`` where it said nothing --
    a mark price or an unrealised PnL that could not be read is not zero, and
    rendering it as zero would put a number on a screen that nothing observed.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    #: Signed: negative is short. The venue's own convention, kept rather than
    #: split into a side and a magnitude, because the sign *is* the side and
    #: re-deriving it is a chance to get it backwards.
    quantity: Decimal
    entry_price: Decimal | None = None
    mark_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    leverage: Decimal | None = None
    margin_mode: MarginMode | None = None

    @property
    def is_open(self) -> bool:
        return self.quantity != 0
