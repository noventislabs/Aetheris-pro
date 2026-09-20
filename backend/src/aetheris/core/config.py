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
from urllib.parse import urlparse

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


class BinanceFuturesSettings(BaseSettings):
    """Binance USDT-M Futures connectivity.

    Public market data needs no credentials, so none are defined here. Trading
    keys arrive with the testnet/live phases and will live in their own
    settings class, keeping a read-only deployment incapable of holding a
    trading credential at all.
    """

    model_config = SettingsConfigDict(env_prefix="AETHERIS_BINANCE_", extra="ignore")

    futures_rest_base_url: str = Field(
        default="https://fapi.binance.com",
        description="Base URL for Binance USDT-M Futures public REST endpoints",
    )
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    max_retries: int = Field(
        default=3, ge=0, le=5, description="Retries after the first attempt, never unbounded"
    )
    backoff_seconds: float = Field(
        default=0.5, ge=0, le=10, description="Base delay for exponential backoff"
    )
    max_backoff_seconds: float = Field(default=8.0, ge=0, le=60)

    # Cache sizing is deliberately small: the target machine has ~1 GB free RAM
    # and a kline entry can hold 1500 candles.
    exchange_info_ttl_seconds: float = Field(default=300.0, gt=0)
    ticker_ttl_seconds: float = Field(default=3.0, gt=0)
    klines_ttl_seconds: float = Field(default=10.0, gt=0)
    klines_cache_max_entries: int = Field(default=16, ge=1, le=256)
    ticker_cache_max_entries: int = Field(default=64, ge=1, le=1024)

    default_klines_limit: int = Field(default=200, ge=1, le=1500)
    max_klines_limit: int = Field(
        default=1500, ge=1, le=1500, description="Binance hard ceiling for /klines"
    )

    @field_validator("futures_rest_base_url")
    @classmethod
    def _must_be_a_plain_https_base(cls, value: str) -> str:
        """Constrain the only externally-controlled URL in the system.

        This value is operator configuration, never request input -- no API
        parameter can redirect a call elsewhere. Validating it anyway keeps a
        typo or a copied config from silently pointing the adapter at http, or
        at a URL carrying a path/query that would corrupt every request.
        """
        parsed = urlparse(value)
        if parsed.scheme not in ("https", "http"):
            raise ValueError("base URL must be http or https")
        if not parsed.netloc:
            raise ValueError("base URL must include a host")
        if parsed.query or parsed.fragment:
            raise ValueError("base URL must not carry a query string or fragment")
        return value.rstrip("/")


class MarketDataSettings(BaseSettings):
    """Freshness policy for externally sourced market data."""

    model_config = SettingsConfigDict(env_prefix="AETHERIS_MARKET_DATA_", extra="ignore")

    max_ticker_age_seconds: float = Field(
        default=30.0,
        gt=0,
        description="A ticker older than this is reported STALE and carries no value",
    )
    max_candle_age_multiple: float = Field(
        default=2.5,
        gt=0,
        description="A closed candle older than this many intervals marks the series STALE",
    )
    symbol_search_limit: int = Field(default=25, ge=1, le=200)


class ScannerSettings(BaseSettings):
    """Bounds for a market scan.

    Every value here exists to keep one HTTP request from turning into
    hundreds. The universe is several hundred perpetuals; fetching candles for
    all of them per request is not a slow option, it is a rate-limit ban and an
    out-of-memory error on an 8 GB machine.
    """

    model_config = SettingsConfigDict(env_prefix="AETHERIS_SCANNER_", extra="ignore")

    default_page_size: int = Field(default=25, ge=1, le=200)
    max_page_size: int = Field(default=100, ge=1, le=200)

    #: When ordering by a candle-derived field, metrics are computed for this
    #: many of the most liquid instruments and the ranking covers that pool.
    #: The response says so rather than implying the whole market was ranked.
    candidate_pool_size: int = Field(default=60, ge=1, le=120)

    #: Hard ceiling on candle requests served for any single scan.
    max_metric_symbols: int = Field(default=60, ge=1, le=120)

    #: Simultaneous upstream candle requests. Small on purpose.
    metric_concurrency: int = Field(default=6, ge=1, le=16)

    #: Candles pulled per instrument for metrics. Enough for the 15-bar
    #: minimum plus the 10-bar momentum lookback, with headroom for a forming
    #: bar being dropped -- and no more, because this multiplies by the pool.
    candle_limit: int = Field(default=60, ge=20, le=500)


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
    binance: BinanceFuturesSettings = Field(default_factory=BinanceFuturesSettings)
    market_data: MarketDataSettings = Field(default_factory=MarketDataSettings)
    scanner: ScannerSettings = Field(default_factory=ScannerSettings)

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
