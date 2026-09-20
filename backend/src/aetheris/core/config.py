"""Application configuration.

Settings come from the environment (and a local ``.env`` for development).
Secrets never have a default and never appear in a committed file -- the
repository ships ``.env.example`` with placeholders only.

The defaults encode the product's opening posture: paper money, modest size,
autonomy off, live trading off. Every one of those has to be turned on
deliberately.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aetheris.domain.enums import TradingMode


class RiskSettings(BaseSettings):
    """Default risk envelope. Persisted per-account later; these are the seeds."""

    model_config = SettingsConfigDict(env_prefix="AETHERIS_RISK_", extra="ignore")

    paper_starting_balance: Decimal = Field(
        default=Decimal("100"), description="Opening paper balance in USDT"
    )
    daily_profit_target: Decimal = Field(
        default=Decimal("20"),
        description="Daily PnL at which new entries stop; positions keep being managed",
    )
    daily_loss_limit: Decimal = Field(
        default=Decimal("-10"),
        description="Daily PnL at which entries lock; must be negative",
    )
    max_leverage: Decimal = Field(default=Decimal("3"), description="Hard leverage ceiling")
    max_open_positions: int = Field(default=5, ge=1)
    max_position_notional: Decimal = Field(
        default=Decimal("100"), description="Per-position notional ceiling in USDT"
    )
    max_portfolio_exposure: Decimal = Field(
        default=Decimal("300"), description="Aggregate notional ceiling in USDT"
    )
    entry_cooldown_seconds: int = Field(default=300, ge=0)
    max_data_age_seconds: float = Field(
        default=30.0,
        gt=0,
        description="Market data older than this cannot inform an order decision",
    )

    @field_validator("daily_loss_limit")
    @classmethod
    def _loss_limit_is_negative(cls, value: Decimal) -> Decimal:
        if value >= 0:
            raise ValueError("daily_loss_limit must be negative (it is a PnL floor)")
        return value

    @field_validator("daily_profit_target", "max_leverage", "paper_starting_balance")
    @classmethod
    def _must_be_positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("value must be positive")
        return value


class Settings(BaseSettings):
    """Top-level application settings."""

    model_config = SettingsConfigDict(
        env_prefix="AETHERIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # --- Mode gating (spec sections 3, 15, 16, 17) -------------------------
    default_mode: TradingMode = TradingMode.ANALYSIS
    autonomous_trading_enabled: bool = Field(
        default=False, description="Must be switched on by the user, never by the system"
    )
    paper_trading_enabled: bool = True
    testnet_trading_enabled: bool = False
    live_trading_enabled: bool = False
    live_activation_acknowledged: bool = Field(
        default=False,
        description="Second, independent switch required alongside live_trading_enabled",
    )

    # --- Secrets: no defaults, never logged --------------------------------
    database_url: SecretStr | None = None
    secret_key: SecretStr | None = None

    risk: RiskSettings = Field(default_factory=RiskSettings)

    @model_validator(mode="after")
    def _live_requires_two_switches(self) -> Self:
        """Live trading needs two independent affirmations, not one flag.

        A single env var is too easy to set by copying someone else's file;
        requiring a second, differently-named acknowledgement makes enabling
        live trading a deliberate act.
        """
        if self.live_trading_enabled and not self.live_activation_acknowledged:
            raise ValueError(
                "live_trading_enabled requires live_activation_acknowledged=true; "
                "live trading cannot be enabled by a single flag"
            )
        return self

    @model_validator(mode="after")
    def _production_requires_secrets(self) -> Self:
        if self.environment == "production":
            missing = [
                name for name in ("database_url", "secret_key") if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"production requires {', '.join(missing)} to be set")
        return self

    def is_mode_enabled(self, mode: TradingMode) -> bool:
        """Whether a mode may be entered at all.

        ANALYSIS is always available -- it reads markets and places nothing.
        """
        match mode:
            case TradingMode.ANALYSIS:
                return True
            case TradingMode.PAPER:
                return self.paper_trading_enabled
            case TradingMode.TESTNET:
                return self.testnet_trading_enabled
            case TradingMode.LIVE:
                return self.live_trading_enabled and self.live_activation_acknowledged

    @property
    def enabled_modes(self) -> list[TradingMode]:
        return [mode for mode in TradingMode if self.is_mode_enabled(mode)]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that configuration is read once and stays stable for the life of
    the process; tests clear the cache via ``get_settings.cache_clear()``.
    """
    return Settings()
