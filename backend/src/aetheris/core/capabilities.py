"""Capability registry -- the system's honest self-description.

Spec sections 25 and 38 forbid presenting a planned capability as operational.
Rather than trusting documentation to stay truthful, the truth lives here as
data: one entry per subsystem, each with a status that the API exposes and
Falcon must consult before claiming it can do something.

The rule for maintaining this file: a capability moves to ``AVAILABLE`` only in
the same commit that lands its tests. Never ahead of them.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class CapabilityStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    PLANNED = "PLANNED"

    @property
    def is_operational(self) -> bool:
        return self in (CapabilityStatus.AVAILABLE, CapabilityStatus.PARTIAL)


#: Phases whose work is merged and tested. A capability may only be marked
#: AVAILABLE if its phase appears here, which keeps the registry from
#: drifting ahead of delivery one optimistic edit at a time.
DELIVERED_PHASES: frozenset[int] = frozenset({0, 2, 3})


class Capability(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    status: CapabilityStatus
    phase: int
    detail: str


#: Ordered by development phase (spec section 39). Statuses reflect what is
#: actually merged and tested at this commit -- not what is intended.
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        key="core.configuration",
        name="Configuration and mode gating",
        status=CapabilityStatus.AVAILABLE,
        phase=0,
        detail="Settings, trading-mode gating and risk defaults are implemented and tested.",
    ),
    Capability(
        key="core.money",
        name="Decimal money arithmetic",
        status=CapabilityStatus.AVAILABLE,
        phase=0,
        detail="Exact decimal accounting with exchange tick/step quantisation.",
    ),
    Capability(
        key="core.observability",
        name="Structured logging and health checks",
        status=CapabilityStatus.AVAILABLE,
        phase=0,
        detail="Request-scoped structured logs, liveness and readiness endpoints.",
    ),
    Capability(
        key="auth.identity",
        name="Authentication and users",
        status=CapabilityStatus.PLANNED,
        phase=1,
        detail="Argon2id credentials, sessions and tenant isolation. Not started.",
    ),
    Capability(
        key="persistence.database",
        name="PostgreSQL persistence and migrations",
        status=CapabilityStatus.PLANNED,
        phase=1,
        detail="No database is configured in this build; nothing is persisted across restarts.",
    ),
    Capability(
        key="exchange.abstraction",
        name="Exchange abstraction",
        status=CapabilityStatus.AVAILABLE,
        phase=2,
        detail=(
            "MarketDataPort defines read-only venue access. The trading port is "
            "declared as a type only and implemented by nothing."
        ),
    ),
    Capability(
        key="exchange.binance_futures",
        name="Binance USDT-M Futures public market data",
        status=CapabilityStatus.AVAILABLE,
        phase=2,
        detail=(
            "Public REST market data, read-only. Holds no credentials and has no "
            "code path to an order."
        ),
    ),
    Capability(
        key="market.symbol_discovery",
        name="Dynamic symbol discovery",
        status=CapabilityStatus.AVAILABLE,
        phase=2,
        detail=(
            "Eligible USDT-M perpetuals are discovered from venue metadata; no "
            "symbol list is hardcoded."
        ),
    ),
    Capability(
        key="market.ticker",
        name="Ticker data",
        status=CapabilityStatus.AVAILABLE,
        phase=2,
        detail="Normalized 24h statistics with best bid/ask where the venue publishes it.",
    ),
    Capability(
        key="market.klines",
        name="OHLCV candle data",
        status=CapabilityStatus.AVAILABLE,
        phase=2,
        detail="Validated candles for 1m/5m/15m/1h/4h/1d, REST only. No websocket stream.",
    ),
    Capability(
        key="market.scanner",
        name="Market scanner",
        status=CapabilityStatus.AVAILABLE,
        phase=3,
        detail=(
            "Bounded search, filter, sort and pagination over the discovered "
            "universe, with a deterministic Market Opportunity Score. The score "
            "is a ranking metric, not a probability of profit or a prediction."
        ),
    ),
    Capability(
        key="ui.market_terminal",
        name="Market terminal (Markets + Scanner)",
        status=CapabilityStatus.AVAILABLE,
        phase=3,
        detail=(
            "Read-only Next.js terminal: symbol search, OHLCV chart, scanner table. "
            "Analysis interface only; it has no order entry and stores no credentials."
        ),
    ),
    Capability(
        key="ui.watchlist",
        name="Watchlist",
        status=CapabilityStatus.PARTIAL,
        phase=3,
        detail=(
            "The terminal remembers the selected symbol and timeframe in browser "
            "storage only. This is a per-browser preference, not a persisted "
            "watchlist; server-side persistence needs phase 1."
        ),
    ),
    Capability(
        key="analysis.scanner_metrics",
        name="Scanner candle statistics",
        status=CapabilityStatus.AVAILABLE,
        phase=3,
        detail=(
            "Deterministic volatility, ATR, momentum, trend and relative-volume "
            "statistics over closed candles. Not the full indicator engine."
        ),
    ),
    Capability(
        key="analysis.indicators",
        name="Technical indicator engine",
        status=CapabilityStatus.PLANNED,
        phase=4,
        detail=(
            "Full indicator engine (SMA, EMA, RSI, MACD, Bollinger, VWAP, ADX...). "
            "Not started; the scanner's own statistics are a separate, narrower set."
        ),
    ),
    Capability(
        key="analysis.smc",
        name="Smart Money Concepts engine",
        status=CapabilityStatus.PLANNED,
        phase=4,
        detail="Structure, liquidity and imbalance analysis. Not started.",
    ),
    Capability(
        key="strategy.engine",
        name="Strategy engine",
        status=CapabilityStatus.PLANNED,
        phase=4,
        detail="Modular, independently testable strategies. Not started.",
    ),
    Capability(
        key="backtest.engine",
        name="Backtesting engine",
        status=CapabilityStatus.PLANNED,
        phase=5,
        detail="Historical simulation with fees, slippage and drawdown metrics. Not started.",
    ),
    Capability(
        key="optimize.hyperparameters",
        name="Hyperparameter optimisation",
        status=CapabilityStatus.PLANNED,
        phase=5,
        detail="Parameter search with train/test separation and overfitting warnings.",
    ),
    Capability(
        key="paper.engine",
        name="Paper trading engine",
        status=CapabilityStatus.PLANNED,
        phase=6,
        detail="Simulated fills, fees and PnL. Places no real orders. Not started.",
    ),
    Capability(
        key="risk.engine",
        name="Risk engine",
        status=CapabilityStatus.PLANNED,
        phase=7,
        detail="Final authority over every order decision. Vocabulary defined; engine not built.",
    ),
    Capability(
        key="portfolio.engine",
        name="Portfolio and position accounting",
        status=CapabilityStatus.PLANNED,
        phase=7,
        detail="Balance, margin, exposure and daily PnL tracking. Not started.",
    ),
    Capability(
        key="order.engine",
        name="Order lifecycle and reconciliation",
        status=CapabilityStatus.PLANNED,
        phase=8,
        detail="Idempotent submission and crash recovery. State vocabulary defined only.",
    ),
    Capability(
        key="execution.testnet",
        name="Testnet execution",
        status=CapabilityStatus.PLANNED,
        phase=8,
        detail="Isolated credentials and order records. Not started.",
    ),
    Capability(
        key="ai.analysis",
        name="AI/ML analysis layer",
        status=CapabilityStatus.PLANNED,
        phase=9,
        detail="Regime classification and anomaly detection. No model is bundled.",
    ),
    Capability(
        key="falcon.command_center",
        name="Falcon AI command centre",
        status=CapabilityStatus.PLANNED,
        phase=9,
        detail="Coordination layer over analysis subsystems. Not started.",
    ),
    Capability(
        key="execution.live",
        name="Live trading",
        status=CapabilityStatus.PLANNED,
        phase=10,
        detail="Disabled by default and gated behind two independent switches. Not started.",
    ),
)

CAPABILITIES_BY_KEY: dict[str, Capability] = {c.key: c for c in CAPABILITIES}


def get_capability(key: str) -> Capability | None:
    return CAPABILITIES_BY_KEY.get(key)


def is_operational(key: str) -> bool:
    """Guard for code paths that must refuse to pretend.

    An unknown key is treated as not operational: a typo should fail closed.
    """
    capability = CAPABILITIES_BY_KEY.get(key)
    return capability is not None and capability.status.is_operational
