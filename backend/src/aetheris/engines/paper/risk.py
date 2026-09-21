"""Entry risk checks for paper trading.

**This is the gate.** Every paper order passes through ``check_entry`` before
anything is filled, whether it came from a human clicking a button, a strategy
running autonomously, or an API client. None of them can bypass it, and none of
them can override a refusal — the same authority structure the live risk engine
will have in phase 7, exercised now against simulated money so the shape is
tested before it matters.

Every refusal returns a code from the Phase 0 ``RiskRejectionCode`` vocabulary.
That vocabulary was defined before any engine existed precisely so refusals
would be greppable and switchable-on from the day one was issued.

The checks run in order of authority: mode, then emergency stop, then the daily
session lock, then data quality, then leverage, then size and balance. A trade
refused for a more fundamental reason is not also evaluated against a lesser
one, so the reported code is the *first* thing wrong rather than an arbitrary
one of several.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from aetheris.core.errors import RiskRejectionCode
from aetheris.core.freshness import DataStatus
from aetheris.domain.leverage import LeverageDecision
from aetheris.domain.paper import RiskLockState
from aetheris.engines.paper.state import PaperState

__all__ = ["RiskRefusal", "check_authority", "check_entry", "check_market_data"]


@dataclass(frozen=True, slots=True)
class RiskRefusal:
    """A refusal, with the code and a sentence explaining it."""

    code: RiskRejectionCode
    detail: str


def check_market_data(
    symbol: str,
    *,
    mark_status: DataStatus,
    mark_age_seconds: float | None,
    max_data_age_seconds: float,
) -> RiskRefusal | None:
    """Whether this symbol's price may be acted on. ``None`` means yes.

    A paper fill is only as real as the market data behind it. When that data
    is missing, stale, or of unverifiable age, the answer is a refusal -- never
    a last-known price, a zero, or any other number the engine chose itself.
    """
    if mark_status is not DataStatus.OK:
        return RiskRefusal(
            RiskRejectionCode.STALE_DATA,
            f"Market data for {symbol} is {mark_status.value}; no price to fill against.",
        )
    if mark_age_seconds is None:
        return RiskRefusal(
            RiskRejectionCode.STALE_DATA,
            (
                f"Freshness of {symbol}'s price could not be verified (the venue "
                "supplied no event timestamp), so it cannot be used to fill an order."
            ),
        )
    if mark_age_seconds > max_data_age_seconds:
        return RiskRefusal(
            RiskRejectionCode.STALE_DATA,
            (
                f"{symbol}'s price is {mark_age_seconds:.1f}s old, beyond the "
                f"{max_data_age_seconds:.0f}s limit for acting on it."
            ),
        )
    return None


def check_authority(state: PaperState, *, paper_enabled: bool) -> RiskRefusal | None:
    """Whether this account may open *anything* right now.

    Split out from ``check_entry`` because of an ordering problem found running
    against live data: a size cannot be validated before it is computed, and
    computing it needs a price and a leverage approval. That put the venue's
    step-size check ahead of the emergency stop, so halting the desk and then
    submitting an unsizeable order reported ``EXCHANGE_PRECISION`` -- a lesser,
    incidental reason -- while the emergency stop went unmentioned.

    These three refusals depend on nothing about the order, so the engine asks
    them first and the reported code is the most fundamental thing wrong.
    ``check_entry`` still calls this, so the ordering is a property of the gate
    rather than of the caller remembering to ask twice.
    """
    if not paper_enabled:
        return RiskRefusal(
            RiskRejectionCode.MODE_NOT_ENABLED,
            "Paper trading is not enabled in this deployment's configuration.",
        )

    if state.emergency_stopped:
        return RiskRefusal(
            RiskRejectionCode.EMERGENCY_STOP,
            state.emergency_reason or "An emergency stop is active; no new entries.",
        )

    session = state.session
    if session is not None and session.lock_state.blocks_entries:
        if session.lock_state is RiskLockState.DAILY_PROFIT_TARGET:
            return RiskRefusal(
                RiskRejectionCode.DAILY_PROFIT_TARGET,
                session.lock_reason
                or (
                    f"Daily profit target of {session.profit_target} reached "
                    f"(realised {session.realized_pnl}). Existing positions are still "
                    "managed; no new entries today."
                ),
            )
        return RiskRefusal(
            RiskRejectionCode.DAILY_LOSS_LIMIT,
            session.lock_reason
            or (
                f"Daily loss limit of {session.loss_limit} reached "
                f"(realised {session.realized_pnl}). Existing positions are still "
                "managed; no new entries today."
            ),
        )
    return None


def check_entry(
    state: PaperState,
    *,
    paper_enabled: bool,
    symbol: str,
    quantity: Decimal,
    notional: Decimal,
    margin_required: Decimal,
    mark_status: DataStatus,
    mark_age_seconds: float | None,
    max_data_age_seconds: float,
    leverage: LeverageDecision,
    max_open_positions: int,
    max_position_notional: Decimal,
    max_portfolio_exposure: Decimal,
    available_balance: Decimal,
) -> RiskRefusal | None:
    """Decide whether a new paper position may open. ``None`` means yes."""

    authority_problem = check_authority(state, paper_enabled=paper_enabled)
    if authority_problem is not None:
        return authority_problem

    # Data quality. Also called on its own by the engine, which cannot compute
    # a fill price before it knows the price is usable. Repeating it here is
    # deliberate: this function is the gate, and a gate that assumes its caller
    # already checked is not one.
    data_problem = check_market_data(
        symbol,
        mark_status=mark_status,
        mark_age_seconds=mark_age_seconds,
        max_data_age_seconds=max_data_age_seconds,
    )
    if data_problem is not None:
        return data_problem

    # Leverage. The chain has already run; this is where its verdict binds.
    if not leverage.is_usable or leverage.approved_leverage is None:
        return RiskRefusal(
            RiskRejectionCode.MAX_LEVERAGE,
            f"Leverage was not approved: {leverage.detail}",
        )

    if quantity <= 0:
        return RiskRefusal(RiskRejectionCode.POSITION_SIZE, "Quantity must be greater than zero.")
    if notional <= 0:
        return RiskRefusal(
            RiskRejectionCode.MIN_NOTIONAL, "Order notional must be greater than zero."
        )
    if notional > max_position_notional:
        return RiskRefusal(
            RiskRejectionCode.POSITION_SIZE,
            f"Notional {notional} exceeds the per-position ceiling {max_position_notional}.",
        )

    if symbol in state.positions:
        return RiskRefusal(
            RiskRejectionCode.SYMBOL_LIMIT,
            f"A paper position in {symbol} is already open. This engine holds one "
            "position per symbol; close it before opening another.",
        )
    if len(state.positions) >= max_open_positions:
        return RiskRefusal(
            RiskRejectionCode.MAX_OPEN_POSITIONS,
            f"{len(state.positions)} positions are open, at the ceiling of {max_open_positions}.",
        )

    exposure = sum((position.notional for position in state.positions.values()), Decimal(0))
    if exposure + notional > max_portfolio_exposure:
        return RiskRefusal(
            RiskRejectionCode.PORTFOLIO_EXPOSURE,
            f"Opening this position would take total notional to {exposure + notional}, "
            f"beyond the portfolio ceiling {max_portfolio_exposure}.",
        )

    if margin_required > available_balance:
        return RiskRefusal(
            RiskRejectionCode.INSUFFICIENT_BALANCE,
            f"Margin of {margin_required} exceeds the available balance {available_balance}.",
        )

    return None
