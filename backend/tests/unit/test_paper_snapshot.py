"""Paper state must survive a round trip exactly, or not at all.

The state under test is produced by driving the **real** engine -- opening
positions, closing them, accruing fees and a daily session -- rather than
hand-built. A hand-built fixture would only ever exercise the fields the
author remembered, and the field nobody remembers is exactly the one that
silently fails to persist.

The money assertions use values chosen to break float. A balance that drifts
by a cent per restart is the kind of bug found months later and never
explained.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aetheris.analysis.leverage import resolve_leverage
from aetheris.core.freshness import DataStatus
from aetheris.domain.enums import OrderSide
from aetheris.domain.leverage import LeverageRequest
from aetheris.domain.paper import RiskLockState
from aetheris.engines.paper.engine import (
    MarkPrice,
    PaperEngine,
    PaperEngineConfig,
    SubmitOrderRequest,
)
from aetheris.engines.paper.snapshot import (
    SCHEMA_VERSION,
    SnapshotSchemaMismatch,
    from_snapshot,
    to_snapshot,
)
from aetheris.engines.paper.state import PaperState
from aetheris.engines.paper.store import InMemoryPaperRepository

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def config(**overrides: object) -> PaperEngineConfig:
    base: dict[str, object] = {
        "starting_balance": Decimal(100),
        "daily_profit_target": Decimal(20),
        "daily_loss_limit": Decimal(-10),
        "max_open_positions": 5,
        "max_position_notional": Decimal(100),
        "max_portfolio_exposure": Decimal(300),
        "max_data_age_seconds": 30.0,
    }
    base.update(overrides)
    return PaperEngineConfig(**base)  # type: ignore[arg-type]


def engine(**overrides: object) -> PaperEngine:
    cfg = config(**overrides)
    repository = InMemoryPaperRepository(starting_balance=cfg.starting_balance, now=NOW)
    return PaperEngine(repository, cfg)


def mark(price: str = "80000", *, symbol: str = "BTCUSDT") -> MarkPrice:
    return MarkPrice(
        symbol=symbol,
        status=DataStatus.OK,
        source="venue-adapter:rest",
        last_price=Decimal(price),
        bid_price=None,
        ask_price=None,
        age_seconds=1.0,
    )


def approved():  # type: ignore[no-untyped-def]
    return resolve_leverage(
        LeverageRequest(requested_leverage=Decimal(1), basis="test"),
        exchange_max_leverage=None,
        risk_max_leverage=None,
        risk_engine_available=False,
    )


def buy(
    eng: PaperEngine,
    *,
    symbol: str = "BTCUSDT",
    margin: str = "20",
    price: str = "80000",
    now: datetime = NOW,
):  # type: ignore[no-untyped-def]
    return eng.submit_order(
        SubmitOrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            margin=Decimal(margin),
            client_order_id=f"test-{symbol}-{price}-{now.isoformat()}",
        ),
        now=now,
        mark=mark(price, symbol=symbol),
        filters=None,
        leverage=approved(),
        paper_enabled=True,
        marks={symbol: mark(price, symbol=symbol)},
    )


def lived_in_state() -> PaperState:
    """State with real history: an open position, a closed trade, a session."""
    eng = engine()
    buy(eng, symbol="BTCUSDT", margin="20", price="80000")
    buy(eng, symbol="ETHUSDT", margin="15", price="3000")
    # Close one at a profit so trades, fees and the session are all populated.
    eng.close_position(
        "ETHUSDT",
        now=NOW + timedelta(minutes=30),
        mark=mark("3300", symbol="ETHUSDT"),
        marks={"ETHUSDT": mark("3300", symbol="ETHUSDT")},
    )
    return eng._repository.load()


# ----------------------------------------------------------------------
# Round trip
# ----------------------------------------------------------------------


def test_a_lived_in_account_round_trips_field_for_field() -> None:
    """Driven by the engine, so no field escapes because nobody remembered it."""
    original = lived_in_state()
    assert original.positions, "the fixture must leave an open position"
    assert original.trades, "the fixture must leave a closed trade"
    assert original.session is not None, "the fixture must open a daily session"

    restored = from_snapshot(to_snapshot(original))

    assert restored.account_id == original.account_id
    assert restored.created_at == original.created_at
    assert restored.updated_at == original.updated_at
    assert restored.autonomous_enabled == original.autonomous_enabled
    assert restored.emergency_stopped == original.emergency_stopped
    assert restored.emergency_reason == original.emergency_reason
    assert restored.sequence == original.sequence


def test_every_amount_survives_exactly() -> None:
    """Not approximately. A cent per restart is a bug nobody ever explains."""
    original = lived_in_state()
    restored = from_snapshot(to_snapshot(original))

    assert restored.balance == original.balance
    assert restored.starting_balance == original.starting_balance
    assert restored.realized_pnl == original.realized_pnl
    assert restored.total_fees == original.total_fees
    # And they are still Decimals, not floats that happen to compare equal.
    assert isinstance(restored.balance, Decimal)
    assert isinstance(restored.realized_pnl, Decimal)


def test_a_value_that_breaks_float_survives() -> None:
    """The specific failure a JSON number would cause."""
    original = lived_in_state()
    awkward = Decimal("0.1") + Decimal("0.2")
    original.balance = awkward
    original.total_fees = Decimal("12345.678901234567")

    restored = from_snapshot(to_snapshot(original))
    assert restored.balance == awkward
    assert str(restored.balance) == "0.3"
    assert restored.total_fees == Decimal("12345.678901234567")
    assert restored.balance != Decimal(str(0.1 + 0.2))


def test_open_positions_survive_with_their_levels() -> None:
    original = lived_in_state()
    restored = from_snapshot(to_snapshot(original))

    assert set(restored.positions) == set(original.positions)
    for symbol, position in original.positions.items():
        back = restored.positions[symbol]
        assert back.position_id == position.position_id
        assert back.side == position.side
        assert back.quantity == position.quantity
        assert back.entry_price == position.entry_price
        assert back.notional == position.notional
        assert back.margin == position.margin
        assert back.approved_leverage == position.approved_leverage
        assert back.entry_fee == position.entry_fee
        assert back.opened_at == position.opened_at
        assert back.stop_price == position.stop_price
        assert back.target_price == position.target_price
        assert back.liquidation_price == position.liquidation_price


def test_orders_and_trades_survive() -> None:
    original = lived_in_state()
    restored = from_snapshot(to_snapshot(original))

    assert len(restored.order_log) == len(original.order_log)
    assert len(restored.trades) == len(original.trades)
    for before, after in zip(original.order_log, restored.order_log, strict=True):
        assert after == before
    for before_t, after_t in zip(original.trades, restored.trades, strict=True):
        assert after_t == before_t


def test_the_idempotency_index_is_rebuilt_and_still_matches() -> None:
    """Idempotency reads this index. If it is lost, a replay becomes a new order."""
    original = lived_in_state()
    restored = from_snapshot(to_snapshot(original))

    assert set(restored.orders_by_client_id) == set(original.orders_by_client_id)
    for client_id, order in original.orders_by_client_id.items():
        assert restored.orders_by_client_id[client_id] == order
    # Rebuilt from the log rather than stored twice, so the two cannot disagree.
    assert all(restored.orders_by_client_id[o.client_order_id] is o for o in restored.order_log)


def test_the_daily_session_survives_including_its_lock() -> None:
    original = lived_in_state()
    assert original.session is not None
    original.session.lock_state = RiskLockState.DAILY_PROFIT_TARGET
    original.session.lock_reason = "target reached"
    original.session.locked_at = NOW + timedelta(hours=1)

    restored = from_snapshot(to_snapshot(original))
    assert restored.session is not None
    assert restored.session.session_date == original.session.session_date
    assert restored.session.realized_pnl == original.session.realized_pnl
    assert restored.session.fees == original.session.fees
    assert restored.session.trades_closed == original.session.trades_closed
    assert restored.session.lock_state is RiskLockState.DAILY_PROFIT_TARGET
    assert restored.session.lock_reason == "target reached"
    assert restored.session.locked_at == original.session.locked_at


def test_a_fresh_account_round_trips_too() -> None:
    """The empty case, which the lived-in fixture would never reach."""
    state = InMemoryPaperRepository(starting_balance=Decimal(100), now=NOW).load()
    restored = from_snapshot(to_snapshot(state))
    assert restored.balance == Decimal(100)
    assert restored.positions == {}
    assert restored.order_log == []
    assert restored.trades == []
    assert restored.session is None


# ----------------------------------------------------------------------
# It is genuinely JSON, and genuinely versioned
# ----------------------------------------------------------------------


def test_the_snapshot_is_json_serialisable_without_a_custom_encoder() -> None:
    """It has to cross a database column, so stdlib json must accept it."""
    payload = to_snapshot(lived_in_state())
    text = json.dumps(payload)
    assert from_snapshot(json.loads(text)).balance == payload_balance(payload)


def payload_balance(payload: dict[str, object]) -> Decimal:
    return Decimal(str(payload["balance"]))


def test_money_crosses_as_strings_not_numbers() -> None:
    """A JSON number would be parsed back as a float by anything downstream."""
    payload = to_snapshot(lived_in_state())
    assert isinstance(payload["balance"], str)
    assert isinstance(payload["total_fees"], str)
    for position in payload["positions"].values():  # type: ignore[union-attr]
        assert isinstance(position["quantity"], str)
        assert isinstance(position["entry_price"], str)


def test_a_snapshot_from_another_schema_is_refused_not_half_read() -> None:
    """An account whose balance restored but whose positions vanished is worse
    than one that plainly failed to load."""
    payload = to_snapshot(lived_in_state())
    payload["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(SnapshotSchemaMismatch, match="cannot be read by this build"):
        from_snapshot(payload)


def test_a_snapshot_with_no_version_is_refused() -> None:
    payload = to_snapshot(lived_in_state())
    del payload["schema_version"]
    with pytest.raises(SnapshotSchemaMismatch):
        from_snapshot(payload)


def test_a_truncated_snapshot_is_refused() -> None:
    payload = to_snapshot(lived_in_state())
    payload["balance"] = None
    with pytest.raises(SnapshotSchemaMismatch, match="required account field"):
        from_snapshot(payload)


def test_the_round_trip_is_idempotent() -> None:
    """Writing what was just read must produce the identical document."""
    once = to_snapshot(lived_in_state())
    twice = to_snapshot(from_snapshot(once))
    assert once == twice
