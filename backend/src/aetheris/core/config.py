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

from aetheris.domain.enums import Timeframe, TradingMode


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


class AnalysisSettings(BaseSettings):
    """Bounds for indicator and strategy analysis.

    Indicators are cheap per bar but multiply by the number of indicators
    requested, so both the candle count and the indicator count are capped
    here rather than trusted from the caller.
    """

    model_config = SettingsConfigDict(env_prefix="AETHERIS_ANALYSIS_", extra="ignore")

    default_candle_limit: int = Field(default=300, ge=20, le=1500)
    max_candle_limit: int = Field(default=1000, ge=20, le=1500)
    #: Trailing indicator points a response may carry. Opt-in; a full 1500-bar
    #: MACD is 4500 numbers and would dwarf the rest of the payload.
    max_series_points: int = Field(default=500, ge=0, le=1500)


class BacktestSettings(BaseSettings):
    """Bounds for a historical simulation.

    A backtest is the most expensive read the system serves: it pulls a long
    candle history and walks every bar. The ceiling is the venue's own
    /klines maximum, because fetching more would mean paging and this phase
    does not page.
    """

    model_config = SettingsConfigDict(env_prefix="AETHERIS_BACKTEST_", extra="ignore")

    default_candle_limit: int = Field(default=500, ge=60, le=1500)
    max_candle_limit: int = Field(default=1500, ge=60, le=1500)


class PaperTradingSettings(BaseSettings):
    """Costs and bounds for the paper simulation.

    Separate from ``RiskSettings`` because these describe the *simulator*,
    not the risk envelope. The risk limits apply identically whichever mode
    they guard; a modelled taker fee applies only to a simulated fill.
    """

    model_config = SettingsConfigDict(env_prefix="AETHERIS_PAPER_", extra="ignore")

    taker_fee_bps: Decimal = Field(
        default=Decimal("5"),
        ge=0,
        le=100,
        description="Per side, on notional. Matches the backtest default so the two "
        "simulations are comparable.",
    )
    slippage_bps: Decimal = Field(
        default=Decimal("2"),
        ge=0,
        le=100,
        description="Applied only when the venue publishes no bid/ask to fill against",
    )
    #: In-memory history is bounded because the target machine is small and
    #: these lists would otherwise grow for as long as the process runs.
    max_order_log: int = Field(default=200, ge=10, le=5000)
    max_trade_log: int = Field(default=200, ge=10, le=5000)


class AutonomousSettings(BaseSettings):
    """Bounds for the autonomous paper-trading loop.

    Every default here is chosen so that a deployment which sets nothing does
    nothing: the universe is empty, so an armed loop evaluates no symbols and
    says so. Autonomy that picks its own instruments on first run would be a
    system choosing what to trade before anyone asked it to.
    """

    model_config = SettingsConfigDict(env_prefix="AETHERIS_AUTO_", extra="ignore")

    #: Symbols the loop watches. **Empty by default and deliberately so.**
    #: Nothing is inferred from volume, from a scanner ranking, or from what
    #: happens to be liquid today.
    symbols: list[str] = Field(default_factory=list)

    timeframe: Timeframe = Field(
        default=Timeframe.M15, description="Decision timeframe; closed bars only"
    )
    interval_seconds: float = Field(
        default=30.0,
        ge=5.0,
        le=3600.0,
        description="Between iterations. Governs management latency, not entry frequency.",
    )
    max_symbols: int = Field(
        default=5, ge=1, le=25, description="Hard ceiling on symbols evaluated per iteration"
    )
    candle_limit: int = Field(default=300, ge=60, le=1000)

    #: Percent of *available* balance posted as margin. Matches the backtest
    #: engine's position_size_percent semantic so the two stay comparable.
    position_size_percent: Decimal = Field(default=Decimal("10"), gt=0, le=100)

    #: A request, never an authorisation. Above 1 the constraint chain refuses,
    #: because the venue ceiling is unknown; the default keeps the loop
    #: functional rather than perpetually self-refusing.
    requested_leverage: Decimal = Field(default=Decimal("1"), ge=1, le=500)

    stop_loss_percent: Decimal | None = Field(default=Decimal("2"), gt=0, lt=100)
    take_profit_percent: Decimal | None = Field(default=Decimal("4"), gt=0, lt=100)
    trailing_stop_percent: Decimal | None = Field(default=None, gt=0, lt=100)

    #: Close a position whose strategy bias now opposes it. NEUTRAL never
    #: closes -- indecision is not opposition.
    close_on_signal_flip: bool = True

    #: Measured ATR as a percent of price. Above this, entries are refused with
    #: ABNORMAL_VOLATILITY. A measurement threshold, never a forecast.
    max_atr_percent: Decimal = Field(default=Decimal("15"), gt=0, le=100)

    #: Drop a symbol from the session after this many consecutive failures, so
    #: a delisted or permanently broken instrument stops costing quota.
    max_consecutive_failures: int = Field(default=10, ge=1, le=100)
    #: Iterations in which *every* symbol failed before the loop treats it as a
    #: venue outage and lengthens its interval.
    outage_iterations: int = Field(default=5, ge=1, le=100)
    outage_backoff_multiplier: float = Field(default=4.0, ge=1.0, le=60.0)

    #: In-memory and bounded, like every other log in this build.
    max_decision_log: int = Field(default=500, ge=10, le=5000)

    @field_validator("symbols")
    @classmethod
    def _normalise_symbols(cls, value: list[str]) -> list[str]:
        """Upper-case, de-duplicate, and reject anything that is not a symbol.

        Operator configuration rather than request input, but validated anyway:
        a typo should fail at startup rather than produce a loop quietly
        watching nothing.
        """
        seen: list[str] = []
        for raw in value:
            symbol = raw.strip().upper()
            if not symbol:
                continue
            if not symbol.isalnum():
                raise ValueError(f"{raw!r} is not a valid symbol")
            if symbol not in seen:
                seen.append(symbol)
        return seen


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
    #: Both spellings of the local terminal. A browser treats localhost and
    #: 127.0.0.1 as different origins, and the terminal is routinely opened on
    #: either, so allowing only one silently breaks every request from the other.
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )

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
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)
    paper: PaperTradingSettings = Field(default_factory=PaperTradingSettings)
    autonomous: AutonomousSettings = Field(default_factory=AutonomousSettings)

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
