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


def test_trading_capabilities_are_not_claimed_in_this_build() -> None:
    for key in (
        "paper.engine",
        "risk.engine",
        "order.engine",
        "execution.testnet",
        "execution.live",
        "falcon.command_center",
    ):
        capability = get_capability(key)
        assert capability is not None
        assert capability.status is CapabilityStatus.PLANNED, (
            f"{key} claims {capability.status} but no engine is implemented"
        )


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
