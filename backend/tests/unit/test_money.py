"""Decimal arithmetic must be exact and conservative."""

from __future__ import annotations

from decimal import Decimal

import pytest

from aetheris.core.money import (
    InvalidMoneyError,
    floor_to_step,
    quantize_usdt,
    round_price_to_tick,
    to_decimal,
)


def test_float_is_rejected_outright() -> None:
    with pytest.raises(InvalidMoneyError, match="float is not accepted"):
        to_decimal(0.1)  # type: ignore[arg-type]


def test_string_conversion_is_exact() -> None:
    assert to_decimal("0.1") + to_decimal("0.2") == Decimal("0.3")


def test_garbage_input_raises() -> None:
    with pytest.raises(InvalidMoneyError):
        to_decimal("not-a-number")


@pytest.mark.parametrize(
    ("value", "step", "expected"),
    [
        ("1.2345", "0.001", "1.234"),
        ("10", "0.1", "10.0"),
        ("0.0009", "0.001", "0.000"),
        ("7.999999", "1", "7"),
    ],
)
def test_quantity_truncates_down_never_up(value: str, step: str, expected: str) -> None:
    # Rounding a quantity up could spend funds that are not there.
    assert floor_to_step(value, step) == Decimal(expected)


def test_step_must_be_positive() -> None:
    with pytest.raises(InvalidMoneyError, match="step must be positive"):
        floor_to_step("1", "0")


def test_buy_price_snaps_down_and_sell_snaps_up() -> None:
    # Snapping must never make an order more aggressive than requested.
    assert round_price_to_tick("100.567", "0.1", side_is_buy=True) == Decimal("100.5")
    assert round_price_to_tick("100.521", "0.1", side_is_buy=False) == Decimal("100.6")


def test_price_already_on_tick_is_unchanged() -> None:
    assert round_price_to_tick("100.5", "0.1", side_is_buy=True) == Decimal("100.5")
    assert round_price_to_tick("100.5", "0.1", side_is_buy=False) == Decimal("100.5")


def test_usdt_quantisation_keeps_eight_decimals() -> None:
    assert quantize_usdt("1.123456789") == Decimal("1.12345679")
