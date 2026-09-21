"""The risk engine.

Two things these tests guard.

**Every limit refuses, with the right code, and the most fundamental reason
wins.** Refusal ordering is not cosmetic: phase 6 shipped a defect where an
account-level halt was masked by an incidental sizing problem, and the reported
code sent a reader looking in the wrong place entirely.

**Nothing a strategy thinks can loosen the envelope.** Bias and condition
counts ride along on the proposal for the audit trail and the engine never
reads them. A rule that let an analysis's own reading relax the limit it is
being judged against would not be a limit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aetheris.core.errors import RiskRejectionCode
from aetheris.core.freshness import DataStatus
from aetheris.domain.enums import OrderSide
from aetheris.domain.leverage import LeverageReason
from aetheris.domain.market import SymbolFilters
from aetheris.domain.paper import RiskLockState
from aetheris.engines.risk.engine import evaluate
from aetheris.engines.risk.leverage import derive_risk_max_leverage
from aetheris.engines.risk.policy import (
    RiskAccountView,
    RiskMarketView,
    RiskPolicy,
    RiskProposal,
    RiskVerdict,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def policy(**overrides: object) -> RiskPolicy:
    base: dict[str, object] = {
        "max_open_positions": 5,
        "max_position_notional": Decimal(100),
        "max_portfolio_exposure": Decimal(300),
        "max_leverage": Decimal(3),
        "max_data_age_seconds": 30.0,
        "entry_cooldown_seconds": 300.0,
        "max_atr_percent": Decimal(15),
    }
    base.update(overrides)
    return RiskPolicy(**base)  # type: ignore[arg-type]


def account(**overrides: object) -> RiskAccountView:
    base: dict[str, object] = {
        "balance": Decimal(100),
        "available_balance": Decimal(100),
        "equity": Decimal(100),
        "margin_used": Decimal(0),
        "open_symbols": frozenset(),
        "open_position_count": 0,
        "total_notional": Decimal(0),
        "session_realized_pnl": Decimal(0),
        "daily_profit_target": Decimal(20),
        "daily_loss_limit": Decimal(-10),
        "lock_state": RiskLockState.NONE,
    }
    base.update(overrides)
    return RiskAccountView(**base)  # type: ignore[arg-type]


def market(**overrides: object) -> RiskMarketView:
    base: dict[str, object] = {
        "symbol": "TESTUSDT",
        "status": DataStatus.OK,
        "age_seconds": 1.0,
        "last_price": Decimal(100),
        "atr_percent": Decimal(2),
        "filters": None,
        "exchange_max_leverage": None,
    }
    base.update(overrides)
    return RiskMarketView(**base)  # type: ignore[arg-type]


def proposal(**overrides: object) -> RiskProposal:
    base: dict[str, object] = {
        "symbol": "TESTUSDT",
        "side": OrderSide.BUY,
        "requested_margin": Decimal(20),
        "requested_leverage": Decimal(1),
        "stop_loss_percent": Decimal(2),
    }
    base.update(overrides)
    return RiskProposal(**base)  # type: ignore[arg-type]


def verdict(**kwargs: object) -> RiskVerdict:
    return evaluate(
        kwargs.pop("proposal", None) or proposal(),  # type: ignore[arg-type]
        account=kwargs.pop("account", None) or account(),  # type: ignore[arg-type]
        market=kwargs.pop("market", None) or market(),  # type: ignore[arg-type]
        policy=kwargs.pop("policy", None) or policy(),  # type: ignore[arg-type]
        now=NOW,
    )


def filters(min_notional: str | None = "5", step: str = "0.001") -> SymbolFilters:
    return SymbolFilters(
        tick_size=Decimal("0.01"),
        step_size=Decimal(step),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal(min_notional) if min_notional is not None else None,
    )


# ----------------------------------------------------------------------
# Approval
# ----------------------------------------------------------------------


def test_a_proposal_within_every_limit_is_approved() -> None:
    v = verdict()
    assert v.approved
    assert v.code is None
    assert v.approved_margin is not None
    assert v.approved_quantity is not None
    assert v.leverage.approved_leverage == Decimal(1)


def test_an_approval_lists_every_check_it_performed() -> None:
    """An audit trail that records only the failing check cannot show what passed."""
    v = verdict()
    assert "mode_enabled" in v.checks_performed
    assert "daily_session_lock" in v.checks_performed
    assert "leverage_constraint_chain" in v.checks_performed
    assert v.checks_performed[-1] == "approved"


def test_a_verdict_cannot_be_both_approved_and_refused() -> None:
    from aetheris.domain.leverage import LeverageDecision, LeverageOutcome

    chain = LeverageDecision(
        outcome=LeverageOutcome.REJECTED,
        reason=LeverageReason.INSUFFICIENT_DATA,
        detail="test",
    )
    with pytest.raises(ValueError, match="must not carry a rejection code"):
        RiskVerdict(
            approved=True,
            detail="x",
            leverage=chain,
            code=RiskRejectionCode.COOLDOWN,
            approved_margin=Decimal(1),
        )
    with pytest.raises(ValueError, match="must name a rejection code"):
        RiskVerdict(approved=False, detail="x", leverage=chain)


# ----------------------------------------------------------------------
# Refusal ordering: the most fundamental reason wins
# ----------------------------------------------------------------------


def test_mode_not_enabled_outranks_everything() -> None:
    v = verdict(
        account=account(mode_enabled=False, emergency_stopped=True),
        market=market(status=DataStatus.UNAVAILABLE, last_price=None),
        proposal=proposal(requested_margin=Decimal(-5), requested_leverage=Decimal(400)),
    )
    assert v.code is RiskRejectionCode.MODE_NOT_ENABLED


def test_an_emergency_stop_outranks_a_sizing_problem() -> None:
    """The phase 6 defect, asserted here so it cannot return."""
    v = verdict(
        account=account(emergency_stopped=True, emergency_reason="Operator halt"),
        market=market(filters=filters()),
        proposal=proposal(requested_margin=Decimal("0.0001")),
    )
    assert v.code is RiskRejectionCode.EMERGENCY_STOP
    assert "Operator halt" in v.detail


def test_a_daily_lock_outranks_stale_data() -> None:
    v = verdict(
        account=account(lock_state=RiskLockState.DAILY_LOSS_LIMIT),
        market=market(status=DataStatus.STALE, last_price=None),
    )
    assert v.code is RiskRejectionCode.DAILY_LOSS_LIMIT


def test_stale_data_outranks_the_cooldown() -> None:
    v = verdict(
        account=account(last_entry_at=NOW),
        market=market(status=DataStatus.UNAVAILABLE, last_price=None),
    )
    assert v.code is RiskRejectionCode.STALE_DATA


# ----------------------------------------------------------------------
# Each limit, refusing for its own cause
# ----------------------------------------------------------------------


def test_the_daily_profit_target_refuses_entries() -> None:
    v = verdict(account=account(lock_state=RiskLockState.DAILY_PROFIT_TARGET))
    assert v.code is RiskRejectionCode.DAILY_PROFIT_TARGET
    assert "still managed" in v.detail


def test_the_daily_loss_limit_refuses_entries() -> None:
    v = verdict(account=account(lock_state=RiskLockState.DAILY_LOSS_LIMIT))
    assert v.code is RiskRejectionCode.DAILY_LOSS_LIMIT


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"status": DataStatus.UNAVAILABLE, "last_price": None}, id="unavailable"),
        pytest.param({"status": DataStatus.STALE, "last_price": None}, id="stale"),
        pytest.param({"age_seconds": None}, id="age-unverifiable"),
        pytest.param({"age_seconds": 900.0}, id="too-old"),
    ],
)
def test_unusable_market_data_refuses_the_proposal(bad: dict) -> None:
    assert verdict(market=market(**bad)).code is RiskRejectionCode.STALE_DATA


def test_the_cooldown_fires_inside_its_window() -> None:
    v = verdict(account=account(last_entry_at=NOW - timedelta(seconds=60)))
    assert v.code is RiskRejectionCode.COOLDOWN
    assert "cooldown" in v.detail.lower()


def test_the_cooldown_has_expired_outside_its_window() -> None:
    assert verdict(account=account(last_entry_at=NOW - timedelta(seconds=400))).approved


def test_unmeasurable_volatility_refuses_rather_than_assuming_zero() -> None:
    v = verdict(market=market(atr_percent=None))
    assert v.code is RiskRejectionCode.STALE_DATA
    assert "could not be measured" in v.detail


def test_abnormal_volatility_refuses_and_is_stated_as_a_measurement() -> None:
    v = verdict(market=market(atr_percent=Decimal(40)))
    assert v.code is RiskRejectionCode.ABNORMAL_VOLATILITY
    assert "not a forecast" in v.detail


def test_a_symbol_already_open_is_refused() -> None:
    v = verdict(account=account(open_symbols=frozenset({"TESTUSDT"}), open_position_count=1))
    assert v.code is RiskRejectionCode.SYMBOL_LIMIT


def test_the_open_position_ceiling_is_enforced() -> None:
    v = verdict(
        account=account(open_symbols=frozenset({"A", "B"}), open_position_count=2),
        policy=policy(max_open_positions=2),
    )
    assert v.code is RiskRejectionCode.MAX_OPEN_POSITIONS


def test_margin_beyond_the_available_balance_is_refused() -> None:
    v = verdict(proposal=proposal(requested_margin=Decimal(500)))
    assert v.code is RiskRejectionCode.INSUFFICIENT_BALANCE


def test_a_non_positive_margin_is_refused() -> None:
    v = verdict(proposal=proposal(requested_margin=Decimal(0)))
    assert v.code is RiskRejectionCode.POSITION_SIZE


def test_the_per_position_notional_ceiling_is_enforced() -> None:
    v = verdict(
        proposal=proposal(requested_margin=Decimal(50)),
        policy=policy(max_position_notional=Decimal(10)),
    )
    assert v.code is RiskRejectionCode.POSITION_SIZE


def test_the_portfolio_exposure_ceiling_is_enforced() -> None:
    v = verdict(
        account=account(total_notional=Decimal(290), open_position_count=1),
        policy=policy(max_portfolio_exposure=Decimal(300)),
    )
    assert v.code is RiskRejectionCode.PORTFOLIO_EXPOSURE


def test_a_size_below_one_venue_increment_is_refused() -> None:
    v = verdict(
        proposal=proposal(requested_margin=Decimal("0.01")),
        market=market(last_price=Decimal(80000), filters=filters(step="0.001")),
    )
    assert v.code is RiskRejectionCode.EXCHANGE_PRECISION


def test_a_notional_below_the_venue_minimum_is_refused() -> None:
    v = verdict(
        proposal=proposal(requested_margin=Decimal(10)),
        market=market(filters=filters(min_notional="50")),
    )
    assert v.code is RiskRejectionCode.MIN_NOTIONAL


def test_the_minimum_notional_is_judged_on_the_truncated_quantity() -> None:
    """The size that will actually be submitted is what the ceiling applies to.

    Judging the pre-truncation notional would approve an order the venue then
    rejects, which is the whole class of error honouring filters prevents.
    """
    v = verdict(
        proposal=proposal(requested_margin=Decimal("5.4")),
        market=market(last_price=Decimal(100), filters=filters(min_notional="5.2", step="0.01")),
    )
    # 5.4 / 100 = 0.054 -> truncates to 0.05 -> notional 5.00 < 5.2
    assert v.code is RiskRejectionCode.MIN_NOTIONAL


# ----------------------------------------------------------------------
# Leverage: the engine supplies a ceiling, and 1x is still all that passes
# ----------------------------------------------------------------------


def test_the_engine_derives_and_reports_a_risk_ceiling() -> None:
    v = verdict()
    assert v.risk_max_leverage is not None
    assert v.risk_max_leverage > 0


def test_leverage_above_one_still_fails_closed_without_a_venue_ceiling() -> None:
    """The central phase 7 property. Building the engine unlocks nothing.

    The risk ceiling is now known, but the venue's per-symbol maximum is not --
    it needs an authenticated endpoint this build has no credential for -- so
    the chain refuses exactly as it did before.
    """
    for requested in ("1.5", "2", "3", "10", "125", "500"):
        v = verdict(proposal=proposal(requested_leverage=Decimal(requested)))
        assert v.code is RiskRejectionCode.MAX_LEVERAGE, requested
        assert v.leverage.approved_leverage is None
        assert v.leverage.reason is LeverageReason.EXCHANGE_MAX_UNKNOWN
        assert v.leverage.exchange_max_leverage is None, "never guessed"


def test_one_times_is_approved_at_the_domain_minimum() -> None:
    v = verdict(proposal=proposal(requested_leverage=Decimal(1)))
    assert v.approved
    assert v.leverage.reason is LeverageReason.APPROVED_AT_DOMAIN_MINIMUM
    assert v.leverage.approved_leverage == Decimal(1)


def test_leverage_resolves_in_full_once_a_venue_ceiling_exists() -> None:
    """What changes the day a credential supplies the bracket, and nothing sooner."""
    v = verdict(
        proposal=proposal(requested_leverage=Decimal(2)),
        market=market(exchange_max_leverage=Decimal(20)),
    )
    assert v.approved
    assert v.leverage.approved_leverage == Decimal(2)


def test_the_configured_ceiling_binds_when_it_is_lowest() -> None:
    v = verdict(
        proposal=proposal(requested_leverage=Decimal(10), stop_loss_percent=Decimal(1)),
        market=market(exchange_max_leverage=Decimal(125)),
        policy=policy(max_leverage=Decimal(3)),
    )
    assert v.approved
    assert v.leverage.approved_leverage == Decimal(3)


def test_the_position_is_sized_from_the_approved_leverage_not_the_request() -> None:
    v = verdict(
        proposal=proposal(requested_leverage=Decimal(50), requested_margin=Decimal(20)),
        market=market(exchange_max_leverage=Decimal(125)),
        policy=policy(max_leverage=Decimal(2), max_position_notional=Decimal(1000)),
    )
    assert v.approved
    assert v.approved_notional == Decimal("40.00000000")  # 20 x 2, never 20 x 50


# ----------------------------------------------------------------------
# The ceiling derivation itself
# ----------------------------------------------------------------------


def test_no_stop_distance_means_no_derivation() -> None:
    """None means "could not be derived", never "unlimited"."""
    assert (
        derive_risk_max_leverage(
            stop_loss_percent=None,
            requested_margin=Decimal(20),
            session_realized_pnl=Decimal(0),
            daily_loss_limit=Decimal(-10),
            configured_max_leverage=Decimal(3),
        )
        is None
    )


def test_a_tighter_stop_permits_more_leverage_than_a_wide_one() -> None:
    """The liquidation constraint, in the direction it must run."""

    def ceiling(stop: str) -> Decimal:
        value = derive_risk_max_leverage(
            stop_loss_percent=Decimal(stop),
            requested_margin=Decimal(1),
            session_realized_pnl=Decimal(0),
            daily_loss_limit=Decimal(-1000),
            configured_max_leverage=Decimal(500),
        )
        assert value is not None
        return value

    assert ceiling("1") > ceiling("5") > ceiling("20")


def test_liquidation_stays_outside_the_stop() -> None:
    """At the derived ceiling, the stop must fire before liquidation can."""
    stop = Decimal(4)
    ceiling = derive_risk_max_leverage(
        stop_loss_percent=stop,
        requested_margin=Decimal(1),
        session_realized_pnl=Decimal(0),
        daily_loss_limit=Decimal(-1000),
        configured_max_leverage=Decimal(500),
    )
    assert ceiling is not None
    liquidation_distance = Decimal(100) / ceiling
    assert liquidation_distance > stop, "the stop would be decoration"


def test_the_ceiling_tightens_as_the_day_s_loss_budget_is_spent() -> None:
    """The behaviour a daily limit is for."""
    fresh = derive_risk_max_leverage(
        stop_loss_percent=Decimal(2),
        requested_margin=Decimal(50),
        session_realized_pnl=Decimal(0),
        daily_loss_limit=Decimal(-10),
        configured_max_leverage=Decimal(500),
    )
    late = derive_risk_max_leverage(
        stop_loss_percent=Decimal(2),
        requested_margin=Decimal(50),
        session_realized_pnl=Decimal("-9"),
        daily_loss_limit=Decimal(-10),
        configured_max_leverage=Decimal(500),
    )
    assert fresh is not None and late is not None
    assert late < fresh


def test_the_ceiling_never_exceeds_the_configured_maximum() -> None:
    value = derive_risk_max_leverage(
        stop_loss_percent=Decimal("0.1"),
        requested_margin=Decimal(1),
        session_realized_pnl=Decimal(0),
        daily_loss_limit=Decimal(-100000),
        configured_max_leverage=Decimal(3),
    )
    assert value == Decimal(3)


# ----------------------------------------------------------------------
# Nothing a strategy thinks may loosen the envelope
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "opinion",
    [
        {"bias": "LONG_BIAS", "conditions_met": 4, "conditions_total": 4},
        {"bias": "SHORT_BIAS", "conditions_met": 0, "conditions_total": 4},
        {"bias": None, "conditions_met": None, "conditions_total": None},
    ],
)
def test_strategy_opinion_does_not_change_a_single_verdict(opinion: dict) -> None:
    """Carried for the audit trail, never read.

    A rule that let an analysis's own reading relax the limit it is being
    judged against would not be a limit.
    """
    baseline = verdict(proposal=proposal())
    flavoured = verdict(proposal=proposal(**opinion))
    assert flavoured.approved == baseline.approved
    assert flavoured.approved_margin == baseline.approved_margin
    assert flavoured.approved_quantity == baseline.approved_quantity
    assert flavoured.risk_max_leverage == baseline.risk_max_leverage


def test_a_maximally_confident_looking_proposal_is_still_refused_when_locked() -> None:
    v = verdict(
        account=account(lock_state=RiskLockState.DAILY_LOSS_LIMIT),
        proposal=proposal(bias="LONG_BIAS", conditions_met=4, conditions_total=4),
    )
    assert v.code is RiskRejectionCode.DAILY_LOSS_LIMIT


# ----------------------------------------------------------------------
# Phase 8a: unreconciled orders block entry
#
# RISK_REJECTED_RECONCILIATION_PENDING has been in the vocabulary since phase 0
# and had never fired. This is what it is for.
# ----------------------------------------------------------------------


def test_an_unreconciled_order_blocks_new_entries() -> None:
    """There may be a position at the venue that this system cannot see."""
    v = verdict(account=account(unreconciled_orders=1))
    assert v.code is RiskRejectionCode.RECONCILIATION_PENDING
    assert "cannot see" in v.detail


def test_nothing_unreconciled_does_not_block() -> None:
    assert verdict(account=account(unreconciled_orders=0)).approved


def test_the_order_engine_s_own_wording_is_used_when_supplied() -> None:
    v = verdict(
        account=account(
            unreconciled_orders=2,
            unreconciled_detail="2 order(s) awaiting reconciliation: aeth-abc, aeth-def.",
        )
    )
    assert "aeth-abc" in v.detail


def test_reconciliation_pending_outranks_the_daily_lock() -> None:
    """Deliberate ordering, not an accident of sequence.

    If orders are unreconciled then the day's realised PnL may itself be wrong
    -- a fill this system has not seen is a fill not in the total -- so judging
    the daily budget first would mean judging it against numbers already known
    to be incomplete.
    """
    v = verdict(
        account=account(
            unreconciled_orders=1,
            lock_state=RiskLockState.DAILY_LOSS_LIMIT,
        )
    )
    assert v.code is RiskRejectionCode.RECONCILIATION_PENDING


def test_an_emergency_stop_still_outranks_reconciliation() -> None:
    """A halt is more fundamental than not knowing."""
    v = verdict(account=account(unreconciled_orders=1, emergency_stopped=True))
    assert v.code is RiskRejectionCode.EMERGENCY_STOP


def test_reconciliation_pending_outranks_stale_data_and_sizing() -> None:
    v = verdict(
        account=account(unreconciled_orders=1),
        market=market(status=DataStatus.UNAVAILABLE, last_price=None),
        proposal=proposal(requested_margin=Decimal(-5)),
    )
    assert v.code is RiskRejectionCode.RECONCILIATION_PENDING


def test_the_check_is_recorded_in_the_audit_trail() -> None:
    assert "reconciliation_pending" in verdict().checks_performed


def test_paper_is_unaffected_because_it_has_no_venue_to_disagree_with() -> None:
    """The default is zero, so phase 6 and 7 behaviour is unchanged."""
    from aetheris.engines.risk.policy import RiskAccountView

    default = RiskAccountView(
        balance=Decimal(100),
        available_balance=Decimal(100),
        equity=Decimal(100),
        margin_used=Decimal(0),
        open_symbols=frozenset(),
        open_position_count=0,
        total_notional=Decimal(0),
        session_realized_pnl=Decimal(0),
        daily_profit_target=Decimal(20),
        daily_loss_limit=Decimal(-10),
        lock_state=RiskLockState.NONE,
    )
    assert default.unreconciled_orders == 0
