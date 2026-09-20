"""Exact decimal arithmetic for money, quantities and prices.

Financial accounting in Aetheris never uses binary floating point. Floats are
tolerated only at the edges: an exchange sends JSON numbers, and a chart
consumes them. Everything between those edges is ``Decimal``.

Exchange filters (tick size, step size) are truncation rules, not rounding
rules: submitting a price finer than the tick or a quantity finer than the step
is rejected by the venue, and rounding *up* can breach a balance you do not
have. So quantities always truncate down and prices truncate toward whatever
keeps the order passive-safe for its side.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal, InvalidOperation
from typing import Final

__all__ = [
    "PRICE_DISPLAY_EXPONENT",
    "USDT_EXPONENT",
    "ZERO",
    "InvalidMoneyError",
    "floor_to_step",
    "quantize_usdt",
    "round_price_to_tick",
    "to_decimal",
]

ZERO: Final = Decimal(0)

#: USDT balances and PnL are accounted to 8 dp internally; exchanges settle
#: coarser, but intermediate fee/funding math must not lose precision.
USDT_EXPONENT: Final = Decimal("0.00000001")

#: Display-only precision. Never use for accounting.
PRICE_DISPLAY_EXPONENT: Final = Decimal("0.00000001")


class InvalidMoneyError(ValueError):
    """Raised when a value cannot be represented exactly as a Decimal amount."""


def to_decimal(value: Decimal | int | str) -> Decimal:
    """Convert to ``Decimal`` without ever going through ``float``.

    ``float`` is deliberately rejected rather than accepted-and-converted:
    ``Decimal(0.1)`` is ``0.1000000000000000055511151231257827``, and silently
    admitting that into a balance is exactly the class of bug this module
    exists to prevent. Callers holding a float (JSON from an exchange) must
    pass ``str(value)`` and thereby acknowledge the lossy step.
    """
    if isinstance(value, float):  # pragma: no cover - guarded by type checker too
        raise InvalidMoneyError(
            "float is not accepted for monetary values; pass str(value) instead"
        )
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidMoneyError(f"cannot represent {value!r} as a decimal amount") from exc


def quantize_usdt(value: Decimal | int | str) -> Decimal:
    """Normalise a USDT amount to the internal accounting exponent."""
    return to_decimal(value).quantize(USDT_EXPONENT, rounding=ROUND_HALF_UP)


def floor_to_step(value: Decimal | int | str, step: Decimal | int | str) -> Decimal:
    """Truncate ``value`` down to a multiple of ``step``.

    Used for order quantity against an exchange ``stepSize`` filter. Rounding
    down can only ever make an order smaller, which is the safe direction: it
    may fall under the minimum notional (a rejection the risk engine catches)
    but it can never spend funds that are not there.
    """
    step_d = to_decimal(step)
    if step_d <= ZERO:
        raise InvalidMoneyError(f"step must be positive, got {step_d}")
    value_d = to_decimal(value)
    return (value_d // step_d) * step_d


def round_price_to_tick(
    value: Decimal | int | str,
    tick: Decimal | int | str,
    *,
    side_is_buy: bool,
) -> Decimal:
    """Snap a price to the exchange tick grid, conservatively for the side.

    A buy limit snaps *down* and a sell limit snaps *up*, so snapping never
    silently makes an order more aggressive than the caller asked for.
    """
    tick_d = to_decimal(tick)
    if tick_d <= ZERO:
        raise InvalidMoneyError(f"tick must be positive, got {tick_d}")
    value_d = to_decimal(value)
    rounding = ROUND_DOWN if side_is_buy else ROUND_UP
    return (value_d / tick_d).quantize(Decimal(1), rounding=rounding) * tick_d
