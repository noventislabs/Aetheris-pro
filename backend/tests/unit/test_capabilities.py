"""The capability registry must stay honest about what is built."""

from __future__ import annotations

from aetheris.core.capabilities import (
    CAPABILITIES,
    DELIVERED_PHASES,
    CapabilityStatus,
    get_capability,
    is_operational,
)


def test_keys_are_unique() -> None:
    keys = [c.key for c in CAPABILITIES]
    assert len(keys) == len(set(keys))


def test_unknown_capability_fails_closed() -> None:
    # A typo must not read as "operational".
    assert is_operational("does.not.exist") is False
    assert get_capability("does.not.exist") is None


def test_real_execution_capabilities_are_not_claimed_in_this_build() -> None:
    """Nothing that could move real money may report as built.

    ``paper.engine`` left this list in phase 6 and ``risk.engine`` /
    ``paper.autonomous`` in phase 7, which is the distinction the registry
    exists to make: a simulation that places no order is a delivered
    capability, and so is the authority that refuses one, while anything that
    reaches a venue is not.
    """
    for key in (
        "order.persistence",
        "execution.testnet",
        "execution.live",
        "falcon.command_center",
        "portfolio.engine",
    ):
        capability = get_capability(key)
        assert capability is not None
        assert capability.status is CapabilityStatus.PLANNED, (
            f"{key} claims {capability.status} but no engine is implemented"
        )


def test_the_order_engine_does_not_claim_crash_recovery() -> None:
    """Phase 8a built the protocol. The guarantee needs 8b's durable store.

    Claiming recovery over in-memory records would be the sharpest untruth this
    registry could tell: the thing a recovery pass queries is exactly the thing
    a restart destroys.
    """
    engine = get_capability("order.engine")
    assert engine is not None
    assert engine.status is CapabilityStatus.PARTIAL
    assert "Crash recovery is NOT delivered" in engine.detail
    assert "NO venue" in engine.detail


def test_autonomous_trading_states_that_it_is_off_by_default() -> None:
    """The registry must not imply a loop is running when none is armed."""
    autonomous = get_capability("paper.autonomous")
    assert autonomous is not None
    assert autonomous.status is CapabilityStatus.AVAILABLE
    assert "OFF by default" in autonomous.detail
    assert "NO real order" in autonomous.detail


def test_the_risk_engine_does_not_claim_to_unlock_leverage() -> None:
    """Phase 7 builds the authority; it does not obtain a venue ceiling.

    Claiming otherwise would be the single most misleading thing this registry
    could say, because the chain still refuses above 1x.
    """
    engine = get_capability("risk.engine")
    assert engine is not None
    assert engine.status is CapabilityStatus.AVAILABLE
    assert "above 1x still fails closed" in engine.detail
    assert "authenticated endpoint" in engine.detail


def test_paper_state_durability_is_disclosed_as_partial() -> None:
    """A balance that silently resets is worse than one the user knows resets."""
    durability = get_capability("paper.persistence")
    assert durability is not None
    assert durability.status is CapabilityStatus.PARTIAL
    assert "IN-MEMORY" in durability.detail
    assert "RESETS ON RESTART" in durability.detail


def test_the_paper_engine_states_that_it_places_no_real_order() -> None:
    engine = get_capability("paper.engine")
    assert engine is not None
    assert engine.status is CapabilityStatus.AVAILABLE
    assert "NO real order" in engine.detail


def test_available_capabilities_come_only_from_delivered_phases() -> None:
    """The registry may not run ahead of what has actually shipped.

    DELIVERED_PHASES is edited deliberately when a phase lands, so marking a
    capability available early fails here rather than misleading a user.
    """
    for capability in CAPABILITIES:
        if capability.status is CapabilityStatus.AVAILABLE:
            assert capability.phase in DELIVERED_PHASES, (
                f"{capability.key} claims AVAILABLE but phase "
                f"{capability.phase} has not been delivered"
            )


def test_terminal_is_reported_as_available() -> None:
    terminal = get_capability("ui.market_terminal")
    assert terminal is not None
    assert terminal.status is CapabilityStatus.AVAILABLE


def test_watchlist_is_only_partial() -> None:
    """Browser-local preferences are not a persisted watchlist.

    Claiming AVAILABLE here would tell a user their list is saved when it
    exists only in one browser and disappears when storage is cleared.
    """
    watchlist = get_capability("ui.watchlist")
    assert watchlist is not None
    assert watchlist.status is CapabilityStatus.PARTIAL
    assert "not a persisted" in watchlist.detail


def test_smc_remains_unclaimed() -> None:
    """SMC is still unbuilt in phase 4, and the terminal must still say so.

    Indicators moved to AVAILABLE in the same commit that landed their
    implementation and hand-calculated tests; SMC did not, so it stays PLANNED
    and no overlay is drawn for it.
    """
    capability = get_capability("analysis.smc")
    assert capability is not None
    assert capability.status is CapabilityStatus.PLANNED


def test_indicators_became_available_with_their_implementation() -> None:
    capability = get_capability("analysis.indicators")
    assert capability is not None
    assert capability.status is CapabilityStatus.AVAILABLE
    assert capability.phase == 4
