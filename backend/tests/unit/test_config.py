"""Configuration defaults encode the product's safe opening posture."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from aetheris.core.config import RiskSettings, Settings
from aetheris.domain.enums import TradingMode


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg, arg-type]


def test_spec_defaults() -> None:
    risk = RiskSettings(_env_file=None)  # type: ignore[call-arg]
    assert risk.paper_starting_balance == Decimal("100")
    assert risk.daily_profit_target == Decimal("20")
    assert risk.daily_loss_limit == Decimal("-10")
    assert risk.max_leverage == Decimal("3")


def test_trading_is_off_by_default() -> None:
    settings = _settings()
    assert settings.autonomous_trading_enabled is False
    assert settings.live_trading_enabled is False
    assert settings.testnet_trading_enabled is False
    assert settings.default_mode is TradingMode.ANALYSIS


def test_analysis_and_paper_available_by_default_but_not_testnet_or_live() -> None:
    settings = _settings()
    assert settings.enabled_modes == [TradingMode.ANALYSIS, TradingMode.PAPER]


def test_live_cannot_be_enabled_by_a_single_flag() -> None:
    with pytest.raises(ValidationError, match="live_activation_acknowledged"):
        _settings(live_trading_enabled=True)


def test_live_requires_both_switches() -> None:
    settings = _settings(live_trading_enabled=True, live_activation_acknowledged=True)
    assert settings.is_mode_enabled(TradingMode.LIVE)


def test_acknowledgement_alone_does_not_enable_live() -> None:
    settings = _settings(live_activation_acknowledged=True)
    assert not settings.is_mode_enabled(TradingMode.LIVE)


def test_daily_loss_limit_must_be_negative() -> None:
    with pytest.raises(ValidationError, match="must be negative"):
        RiskSettings(_env_file=None, daily_loss_limit=Decimal("10"))  # type: ignore[call-arg]


def test_production_requires_secrets() -> None:
    with pytest.raises(ValidationError, match="production requires"):
        _settings(environment="production")
