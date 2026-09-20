"""The capability registry must stay honest about what is built."""

from __future__ import annotations

from aetheris.core.capabilities import (
    CAPABILITIES,
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


def test_available_capabilities_are_phase_zero_only() -> None:
    for capability in CAPABILITIES:
        if capability.status is CapabilityStatus.AVAILABLE:
            assert capability.phase == 0
