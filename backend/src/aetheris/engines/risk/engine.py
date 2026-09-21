"""The risk engine. Final authority over every proposal.

**Everything upstream proposes; only this disposes.** A human clicking a
button, the autonomous loop, a strategy, an API client and -- when they exist --
Falcon and the AI layer all arrive at ``evaluate`` with the same authority,
which is none. There is no parameter that relaxes a check, no caller identity
that gets a different answer, and no way to pass a verdict that has already
been decided.

It is pure: no I/O, no framework, no venue, no clock of its own. That is what
makes the whole envelope testable in-process, and it is why the loop cannot
reach an exchange through it.

**Two gates, deliberately.** This engine rules first; the phase 6 paper gate
then re-checks independently before anything fills. That is not redundancy to
be optimised away -- it is the same principle that makes ``resolve_leverage``
re-validate input it was already given. A gate that assumes its caller checked
is not a gate.

Checks run in order of authority, and the **first** thing wrong is what gets
reported. That ordering was bought with a real defect in phase 6: an
account-level halt was being masked by an incidental sizing problem, because
sizing ran first. Here the account-level checks come first by construction.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from aetheris.analysis.leverage import resolve_leverage
from aetheris.core.errors import RiskRejectionCode
from aetheris.core.freshness import DataStatus
from aetheris.core.money import ZERO, floor_to_step, quantize_usdt
from aetheris.domain.leverage import (
    LeverageDecision,
    LeverageOutcome,
    LeverageReason,
    LeverageRequest,
)
from aetheris.domain.paper import RiskLockState
from aetheris.engines.risk.leverage import derive_risk_max_leverage
from aetheris.engines.risk.policy import (
    RiskAccountView,
    RiskMarketView,
    RiskPolicy,
    RiskProposal,
    RiskVerdict,
)

__all__ = ["RISK_ENGINE_VERSION", "evaluate"]

RISK_ENGINE_VERSION = "1.0.0"

_HUNDRED = Decimal(100)


def _refuse(
    code: RiskRejectionCode,
    detail: str,
    leverage: LeverageDecision,
    checks: list[str],
    risk_max: Decimal | None = None,
) -> RiskVerdict:
    return RiskVerdict(
        approved=False,
        code=code,
        detail=detail,
        leverage=leverage,
        risk_max_leverage=risk_max,
        checks_performed=tuple(checks),
    )


def _no_leverage_decision(detail: str) -> LeverageDecision:
    """A placeholder chain for refusals raised before leverage was resolved.

    Reported rather than omitted so a reader can see the chain was not reached,
    instead of wondering whether it was skipped or silently passed.
    """
    return LeverageDecision(
        outcome=LeverageOutcome.REJECTED,
        reason=LeverageReason.INSUFFICIENT_DATA,
        detail=detail,
    )


def evaluate(
    proposal: RiskProposal,
    *,
    account: RiskAccountView,
    market: RiskMarketView,
    policy: RiskPolicy,
    now: datetime,
) -> RiskVerdict:
    """Rule on one proposal. The only entry point, and the only authority."""
    checks: list[str] = []
    symbol = proposal.symbol.upper()
    not_reached = _no_leverage_decision(
        "The leverage chain was not reached: the proposal was refused earlier."
    )

    # ------------------------------------------------------------------
    # 1. Account-level authority. None of these depends on the order, so
    #    they are asked before anything about it is computed.
    # ------------------------------------------------------------------
    checks.append("mode_enabled")
    if not account.mode_enabled:
        return _refuse(
            RiskRejectionCode.MODE_NOT_ENABLED,
            "Paper trading is not enabled in this deployment's configuration.",
            not_reached,
            checks,
        )

    checks.append("emergency_stop")
    if account.emergency_stopped:
        return _refuse(
            RiskRejectionCode.EMERGENCY_STOP,
            account.emergency_reason or "An emergency stop is active; no new entries.",
            not_reached,
            checks,
        )

    checks.append("daily_session_lock")
    if account.lock_state.blocks_entries:
        code = (
            RiskRejectionCode.DAILY_PROFIT_TARGET
            if account.lock_state is RiskLockState.DAILY_PROFIT_TARGET
            else RiskRejectionCode.DAILY_LOSS_LIMIT
        )
        target = (
            account.daily_profit_target
            if code is RiskRejectionCode.DAILY_PROFIT_TARGET
            else account.daily_loss_limit
        )
        return _refuse(
            code,
            account.lock_reason
            or (
                f"The day's realised PnL of {account.session_realized_pnl} USDT has "
                f"reached {target} USDT. No new entries today; open positions are "
                "still managed."
            ),
            not_reached,
            checks,
        )

    # ------------------------------------------------------------------
    # 2. Data quality. Nothing downstream can be computed from a price that
    #    may not be used, so this precedes every derivation.
    # ------------------------------------------------------------------
    checks.append("market_data_usable")
    if market.status is not DataStatus.OK or market.last_price is None:
        return _refuse(
            RiskRejectionCode.STALE_DATA,
            f"Market data for {symbol} is {market.status.value}; no price to act on.",
            not_reached,
            checks,
        )
    if market.age_seconds is None:
        return _refuse(
            RiskRejectionCode.STALE_DATA,
            (
                f"Freshness of {symbol}'s price could not be verified (the venue "
                "supplied no event timestamp), so it cannot inform an order."
            ),
            not_reached,
            checks,
        )
    if market.age_seconds > policy.max_data_age_seconds:
        return _refuse(
            RiskRejectionCode.STALE_DATA,
            (
                f"{symbol}'s price is {market.age_seconds:.1f}s old, beyond the "
                f"{policy.max_data_age_seconds:.0f}s limit for acting on it."
            ),
            not_reached,
            checks,
        )

    # ------------------------------------------------------------------
    # 3. Cooldown and measured volatility.
    # ------------------------------------------------------------------
    checks.append("entry_cooldown")
    if account.last_entry_at is not None and policy.entry_cooldown_seconds > 0:
        elapsed = (now - account.last_entry_at).total_seconds()
        if elapsed < policy.entry_cooldown_seconds:
            return _refuse(
                RiskRejectionCode.COOLDOWN,
                (
                    f"{symbol} was entered {elapsed:.0f}s ago; the cooldown is "
                    f"{policy.entry_cooldown_seconds:.0f}s. Re-entering immediately "
                    "after an exit is how one bad signal becomes several."
                ),
                not_reached,
                checks,
            )

    checks.append("measured_volatility")
    if market.atr_percent is None:
        return _refuse(
            RiskRejectionCode.STALE_DATA,
            (
                f"Volatility for {symbol} could not be measured from the available "
                "candles, so the risk envelope cannot be applied to it."
            ),
            not_reached,
            checks,
        )
    if market.atr_percent > policy.max_atr_percent:
        return _refuse(
            RiskRejectionCode.ABNORMAL_VOLATILITY,
            (
                f"Measured ATR is {market.atr_percent}% of price, beyond the "
                f"{policy.max_atr_percent}% ceiling. This is an observation of "
                "current volatility, not a forecast of what it will do next."
            ),
            not_reached,
            checks,
        )

    # ------------------------------------------------------------------
    # 4. Position slots.
    # ------------------------------------------------------------------
    checks.append("symbol_not_already_open")
    if symbol in account.open_symbols:
        return _refuse(
            RiskRejectionCode.SYMBOL_LIMIT,
            f"A position in {symbol} is already open; this engine holds one per symbol.",
            not_reached,
            checks,
        )

    checks.append("max_open_positions")
    if account.open_position_count >= policy.max_open_positions:
        return _refuse(
            RiskRejectionCode.MAX_OPEN_POSITIONS,
            f"{account.open_position_count} positions are open, at the ceiling of "
            f"{policy.max_open_positions}.",
            not_reached,
            checks,
        )

    # ------------------------------------------------------------------
    # 5. Leverage. This engine supplies the risk ceiling the chain has always
    #    reported as missing -- and the chain still refuses above 1x, because
    #    the venue ceiling remains unknown without an authenticated endpoint.
    # ------------------------------------------------------------------
    checks.append("risk_max_leverage_derived")
    risk_max = derive_risk_max_leverage(
        stop_loss_percent=proposal.stop_loss_percent,
        requested_margin=proposal.requested_margin,
        session_realized_pnl=account.session_realized_pnl,
        daily_loss_limit=account.daily_loss_limit,
        configured_max_leverage=policy.max_leverage,
    )

    checks.append("leverage_constraint_chain")
    decision = resolve_leverage(
        LeverageRequest(
            requested_leverage=proposal.requested_leverage,
            basis=(
                "Risk engine evaluation of a proposed position. The ceiling is derived "
                "from stop distance, posted margin and the day's remaining loss budget "
                "-- measured quantities only. It is not a confidence, a probability, or "
                "an opinion about the trade."
            ),
            inputs={
                "requested_leverage": proposal.requested_leverage,
                "stop_loss_percent": proposal.stop_loss_percent or ZERO,
                "requested_margin": proposal.requested_margin,
            },
        ),
        exchange_max_leverage=market.exchange_max_leverage,
        risk_max_leverage=risk_max,
        risk_engine_available=True,
    )
    if not decision.is_usable or decision.approved_leverage is None:
        return _refuse(
            RiskRejectionCode.MAX_LEVERAGE,
            f"Leverage was not approved: {decision.detail}",
            decision,
            checks,
            risk_max,
        )
    approved_leverage = decision.approved_leverage

    # ------------------------------------------------------------------
    # 6. Sizing, against the venue's real published filters.
    # ------------------------------------------------------------------
    checks.append("margin_positive")
    if proposal.requested_margin <= ZERO:
        return _refuse(
            RiskRejectionCode.POSITION_SIZE,
            "The proposed margin is not positive.",
            decision,
            checks,
            risk_max,
        )

    checks.append("sufficient_balance")
    if proposal.requested_margin > account.available_balance:
        return _refuse(
            RiskRejectionCode.INSUFFICIENT_BALANCE,
            f"Margin of {proposal.requested_margin} exceeds the available balance "
            f"{account.available_balance}.",
            decision,
            checks,
            risk_max,
        )

    notional = quantize_usdt(proposal.requested_margin * approved_leverage)
    quantity = notional / market.last_price

    checks.append("venue_filters")
    if market.filters is not None:
        quantity = floor_to_step(quantity, market.filters.step_size)
        if quantity <= ZERO:
            return _refuse(
                RiskRejectionCode.EXCHANGE_PRECISION,
                (
                    f"After truncating to the venue's step size of "
                    f"{market.filters.step_size}, the order rounds to zero: the "
                    "proposed size is smaller than one tradable increment."
                ),
                decision,
                checks,
                risk_max,
            )
        if market.filters.min_quantity > ZERO and quantity < market.filters.min_quantity:
            return _refuse(
                RiskRejectionCode.EXCHANGE_PRECISION,
                f"Quantity {quantity} is below the venue's minimum of "
                f"{market.filters.min_quantity}.",
                decision,
                checks,
                risk_max,
            )
        # Recomputed from the truncated quantity: the size that will actually
        # be submitted is what every remaining ceiling must be judged against.
        notional = quantize_usdt(quantity * market.last_price)
        if market.filters.min_notional is not None and notional < market.filters.min_notional:
            return _refuse(
                RiskRejectionCode.MIN_NOTIONAL,
                f"Notional {notional} is below the venue's published minimum of "
                f"{market.filters.min_notional} for {symbol}.",
                decision,
                checks,
                risk_max,
            )

    checks.append("max_position_notional")
    if notional > policy.max_position_notional:
        return _refuse(
            RiskRejectionCode.POSITION_SIZE,
            f"Notional {notional} exceeds the per-position ceiling {policy.max_position_notional}.",
            decision,
            checks,
            risk_max,
        )

    checks.append("portfolio_exposure")
    if account.total_notional + notional > policy.max_portfolio_exposure:
        return _refuse(
            RiskRejectionCode.PORTFOLIO_EXPOSURE,
            f"This position would take total notional to "
            f"{account.total_notional + notional}, beyond the portfolio ceiling "
            f"{policy.max_portfolio_exposure}.",
            decision,
            checks,
            risk_max,
        )

    approved_margin = quantize_usdt(notional / approved_leverage)
    checks.append("approved")
    return RiskVerdict(
        approved=True,
        detail=(
            f"Approved: {quantity} {symbol} at {approved_leverage}x, margin "
            f"{approved_margin} USDT, notional {notional} USDT. Every configured "
            "limit was evaluated and none binds."
        ),
        leverage=decision,
        approved_margin=approved_margin,
        approved_quantity=quantity,
        approved_notional=notional,
        risk_max_leverage=risk_max,
        checks_performed=tuple(checks),
    )
