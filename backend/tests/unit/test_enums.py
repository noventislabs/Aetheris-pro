"""The domain vocabulary is a contract; these tests pin its semantics."""

from __future__ import annotations

from aetheris.domain.enums import OrderSide, OrderState, Timeframe, TradingMode


def test_testnet_counts_as_placing_real_orders() -> None:
    # Testnet hits a real matching engine, so it must not be treated as a
    # simulation the way PAPER is.
    assert TradingMode.TESTNET.places_real_orders
    assert not TradingMode.TESTNET.risks_real_funds


def test_only_live_risks_real_funds() -> None:
    assert TradingMode.LIVE.risks_real_funds
    assert not TradingMode.PAPER.places_real_orders
    assert not TradingMode.ANALYSIS.places_real_orders


def test_unknown_and_reconciling_are_not_terminal() -> None:
    # Treating an unknown order as finished is how duplicate positions happen.
    assert not OrderState.UNKNOWN.is_terminal
    assert not OrderState.RECONCILING.is_terminal


def test_terminal_states() -> None:
    assert OrderState.FILLED.is_terminal
    assert OrderState.CANCELLED.is_terminal
    assert OrderState.REJECTED.is_terminal
    assert OrderState.EXPIRED.is_terminal


def test_partially_filled_is_open() -> None:
    assert OrderState.PARTIALLY_FILLED.is_open
    assert OrderState.CANCEL_REQUESTED.is_open
    assert not OrderState.FILLED.is_open


def test_side_opposite() -> None:
    assert OrderSide.BUY.opposite is OrderSide.SELL
    assert OrderSide.SELL.opposite is OrderSide.BUY


def test_timeframe_seconds() -> None:
    assert Timeframe.M1.seconds == 60
    assert Timeframe.H4.seconds == 14_400
    assert all(tf.seconds > 0 for tf in Timeframe)
