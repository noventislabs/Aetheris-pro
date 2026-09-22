"""Serialising paper account state, exactly.

Pure: a ``PaperState`` in, a JSON-safe dict out, and back again with nothing
altered. No I/O, no database, no framework -- so the round-trip can be tested
as arithmetic rather than through a connection.

## Why a snapshot rather than tables

Order records get typed columns because they are evidence: a venue may
disagree with them, a recovery pass queries them, and a discrepancy has to be
provable. Paper state is none of that. It is simulation state owned entirely
by this process, reset wholesale by ``POST /paper/reset``, and never compared
against an outside authority. Modelling it as a dozen related tables would buy
query shapes nobody needs and add a migration to every future engine field.

One versioned row per account, replaced atomically, matches how the state is
actually used. ``SCHEMA_VERSION`` is what makes that safe: a snapshot written
by a different shape is refused rather than half-read.

## Money

Every ``Decimal`` crosses as a **string**. A float would silently round
0.1 + 0.2, and a paper balance that drifts by a cent per restart is the kind
of bug that is noticed months later and never explained. The round-trip test
asserts exactness on values chosen to break float, not on round numbers.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from aetheris.domain.paper import PaperOrder, PaperTrade, RiskLockState
from aetheris.domain.thesis import PositionThesis
from aetheris.engines.paper.state import MutablePosition, MutableSession, PaperState

__all__ = ["SCHEMA_VERSION", "SnapshotSchemaMismatch", "from_snapshot", "to_snapshot"]

#: Bump whenever the shape below changes. A snapshot written under a different
#: version is refused, never partially applied: a half-restored account would
#: report a balance that no sequence of trades produced.
#:
#: v2 added the original trade thesis and the observed excursion extremes. A v1
#: snapshot is refused rather than loaded with those absent, because the
#: intelligence layer reads a missing thesis as evidence that no rule set was
#: involved -- which is a claim about the trade, not about the record. Silently
#: restoring old positions would make that claim on their behalf.
SCHEMA_VERSION: Final = 2


class SnapshotSchemaMismatch(ValueError):
    """A stored snapshot does not match the shape this build can read."""


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _undec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _dt(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _undt(value: Any) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _position_out(position: MutablePosition) -> dict[str, Any]:
    return {
        "position_id": position.position_id,
        "symbol": position.symbol,
        "side": position.side,
        "quantity": _dec(position.quantity),
        "entry_price": _dec(position.entry_price),
        "notional": _dec(position.notional),
        "margin": _dec(position.margin),
        "approved_leverage": _dec(position.approved_leverage),
        "entry_fee": _dec(position.entry_fee),
        "opened_at": _dt(position.opened_at),
        "updated_at": _dt(position.updated_at),
        "opening_order_id": position.opening_order_id,
        "stop_price": _dec(position.stop_price),
        "target_price": _dec(position.target_price),
        "trailing_stop_percent": _dec(position.trailing_stop_percent),
        "trail_extreme": _dec(position.trail_extreme),
        "liquidation_price": _dec(position.liquidation_price),
        "mark_price": _dec(position.mark_price),
        "mark_source": position.mark_source,
        "mark_status": position.mark_status,
        "best_price": _dec(position.best_price),
        "worst_price": _dec(position.worst_price),
        "thesis": (None if position.thesis is None else position.thesis.model_dump(mode="json")),
    }


def _position_in(raw: dict[str, Any]) -> MutablePosition:
    opened = _undt(raw["opened_at"])
    updated = _undt(raw["updated_at"])
    quantity = _undec(raw["quantity"])
    entry = _undec(raw["entry_price"])
    notional = _undec(raw["notional"])
    margin = _undec(raw["margin"])
    leverage = _undec(raw["approved_leverage"])
    fee = _undec(raw["entry_fee"])
    if (
        opened is None
        or updated is None
        or quantity is None
        or entry is None
        or notional is None
        or margin is None
        or leverage is None
        or fee is None
    ):
        raise SnapshotSchemaMismatch("a position is missing a required field")
    return MutablePosition(
        position_id=str(raw["position_id"]),
        symbol=str(raw["symbol"]),
        side=str(raw["side"]),
        quantity=quantity,
        entry_price=entry,
        notional=notional,
        margin=margin,
        approved_leverage=leverage,
        entry_fee=fee,
        opened_at=opened,
        updated_at=updated,
        opening_order_id=str(raw["opening_order_id"]),
        stop_price=_undec(raw.get("stop_price")),
        target_price=_undec(raw.get("target_price")),
        trailing_stop_percent=_undec(raw.get("trailing_stop_percent")),
        trail_extreme=_undec(raw.get("trail_extreme")),
        liquidation_price=_undec(raw.get("liquidation_price")),
        mark_price=_undec(raw.get("mark_price")),
        mark_source=raw.get("mark_source"),
        mark_status=raw.get("mark_status"),
        best_price=_undec(raw.get("best_price")),
        worst_price=_undec(raw.get("worst_price")),
        thesis=(
            PositionThesis.model_validate(raw["thesis"]) if raw.get("thesis") is not None else None
        ),
    )


def _session_out(session: MutableSession) -> dict[str, Any]:
    return {
        "session_date": session.session_date.isoformat(),
        "profit_target": _dec(session.profit_target),
        "loss_limit": _dec(session.loss_limit),
        "realized_pnl": _dec(session.realized_pnl),
        "fees": _dec(session.fees),
        "trades_closed": session.trades_closed,
        "orders_submitted": session.orders_submitted,
        "orders_rejected": session.orders_rejected,
        "lock_state": session.lock_state.value,
        "lock_reason": session.lock_reason,
        "locked_at": _dt(session.locked_at),
    }


def _session_in(raw: dict[str, Any]) -> MutableSession:
    target = _undec(raw["profit_target"])
    limit = _undec(raw["loss_limit"])
    realized = _undec(raw["realized_pnl"])
    fees = _undec(raw["fees"])
    if target is None or limit is None or realized is None or fees is None:
        raise SnapshotSchemaMismatch("the session is missing a required amount")
    return MutableSession(
        session_date=date.fromisoformat(str(raw["session_date"])),
        profit_target=target,
        loss_limit=limit,
        realized_pnl=realized,
        fees=fees,
        trades_closed=int(raw["trades_closed"]),
        orders_submitted=int(raw["orders_submitted"]),
        orders_rejected=int(raw["orders_rejected"]),
        lock_state=RiskLockState(raw["lock_state"]),
        lock_reason=raw.get("lock_reason"),
        locked_at=_undt(raw.get("locked_at")),
    )


def to_snapshot(state: PaperState) -> dict[str, Any]:
    """Render one account's whole state as a JSON-safe dict.

    ``order_log`` and ``orders_by_client_id`` hold the same frozen orders, so
    the log is written in full and the index is rebuilt from it on the way
    back rather than stored twice. Storing both would let them disagree, and
    the index is what idempotency reads.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "account_id": state.account_id,
        "created_at": _dt(state.created_at),
        "updated_at": _dt(state.updated_at),
        "starting_balance": _dec(state.starting_balance),
        "balance": _dec(state.balance),
        "realized_pnl": _dec(state.realized_pnl),
        "total_fees": _dec(state.total_fees),
        "autonomous_enabled": state.autonomous_enabled,
        "emergency_stopped": state.emergency_stopped,
        "emergency_reason": state.emergency_reason,
        "sequence": state.sequence,
        "positions": {
            symbol: _position_out(position) for symbol, position in state.positions.items()
        },
        "order_log": [order.model_dump(mode="json") for order in state.order_log],
        "trades": [trade.model_dump(mode="json") for trade in state.trades],
        "session": _session_out(state.session) if state.session is not None else None,
    }


def from_snapshot(payload: dict[str, Any]) -> PaperState:
    """Rebuild state from a snapshot, or refuse it.

    Refusal is the point of the version check. A snapshot from a different
    shape might load with some fields defaulted, and an account whose balance
    is real but whose open positions silently vanished is far worse than one
    that plainly failed to load.
    """
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise SnapshotSchemaMismatch(
            f"snapshot schema {version!r} cannot be read by this build, which writes "
            f"{SCHEMA_VERSION}. Refusing rather than partially restoring an account."
        )

    created = _undt(payload["created_at"])
    updated = _undt(payload["updated_at"])
    starting = _undec(payload["starting_balance"])
    balance = _undec(payload["balance"])
    realized = _undec(payload["realized_pnl"])
    fees = _undec(payload["total_fees"])
    if (
        created is None
        or updated is None
        or starting is None
        or balance is None
        or realized is None
        or fees is None
    ):
        raise SnapshotSchemaMismatch("the snapshot is missing a required account field")

    order_log = [PaperOrder.model_validate(raw) for raw in payload.get("order_log", [])]
    state = PaperState(
        account_id=str(payload["account_id"]),
        created_at=created,
        updated_at=updated,
        starting_balance=starting,
        balance=balance,
        realized_pnl=realized,
        total_fees=fees,
        autonomous_enabled=bool(payload.get("autonomous_enabled", False)),
        emergency_stopped=bool(payload.get("emergency_stopped", False)),
        emergency_reason=payload.get("emergency_reason"),
        positions={
            symbol: _position_in(raw) for symbol, raw in payload.get("positions", {}).items()
        },
        orders_by_client_id={order.client_order_id: order for order in order_log},
        order_log=order_log,
        trades=[PaperTrade.model_validate(raw) for raw in payload.get("trades", [])],
        session=(_session_in(payload["session"]) if payload.get("session") is not None else None),
        sequence=int(payload.get("sequence", 0)),
    )
    return state
