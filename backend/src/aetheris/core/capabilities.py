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
#:
#: It gates AVAILABLE and nothing else, so a phase that shipped real code but
#: is not finished stays out. Phases 1 and 8 are both in that position: each
#: has running code and each holds PARTIAL capabilities that state how far it
#: actually goes. Listing them would not make the registry more accurate -- no
#: entry's status would change -- it would only remove this guard from every
#: future edit in those phases.
DELIVERED_PHASES: frozenset[int] = frozenset({0, 2, 3, 4, 5, 6, 7})


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
        status=CapabilityStatus.PARTIAL,
        phase=1,
        detail=(
            "PostgreSQL persistence and Alembic migrations 0001-0004 are implemented: "
            "users, accounts, orders, order_fills and order_discrepancies, with "
            "NUMERIC(24,8) money, timestamptz time and row-level security. Durability is "
            "CONDITIONAL on configuration: without DATABASE_URL there is no store at all "
            "-- readiness reports NOT_CONFIGURED, the order lifecycle is absent, and "
            "nothing claims crash recovery. There is deliberately no in-memory fallback. "
            "Scope is order records and the accounts they belong to: the PAPER ACCOUNT "
            "BALANCE, POSITIONS, DAY SESSION AND EMERGENCY-STOP FLAG ARE STILL IN MEMORY "
            "AND STILL RESET ON RESTART. PARTIAL, not AVAILABLE: phase 1's authentication "
            "is not built -- the users table exists, Argon2id credentials and sessions "
            "do not."
        ),
    ),
    Capability(
        key="exchange.abstraction",
        name="Exchange abstraction",
        status=CapabilityStatus.AVAILABLE,
        phase=2,
        detail=(
            "MarketDataPort defines read-only venue access. TradingPort is implemented "
            "by BinanceTestnetTradingAdapter alone, which reports TESTNET and reaches "
            "demo-fapi.binance.com only. NO adapter reports LIVE, and an architecture "
            "test asserts that none does."
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
        status=CapabilityStatus.AVAILABLE,
        phase=4,
        detail=(
            "SMA, EMA, Bollinger, VWAP, RSI, MACD, Stochastic, ATR, ADX, ROC and CCI. "
            "Each states its convention, declares its warm-up, and is verified against "
            "hand-calculated values and a no-look-ahead regression test."
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
        name="Strategy analysis engine",
        status=CapabilityStatus.AVAILABLE,
        phase=4,
        detail=(
            "Deterministic rule-based analysis with one registered strategy "
            "(Trend-Momentum Confluence). Produces an explained bias, never an order: "
            "there is no execution path, and no confidence or probability is invented."
        ),
    ),
    Capability(
        key="backtest.engine",
        name="Backtesting engine",
        status=CapabilityStatus.AVAILABLE,
        phase=5,
        detail=(
            "Historical simulation with fees, slippage, stops, targets, trailing stops "
            "and a simplified liquidation model. Signals fill at the next bar's open "
            "and intrabar ambiguity always resolves against the trade. Funding, "
            "partial fills and maintenance-margin tiers are NOT modelled and are "
            "listed on every result."
        ),
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
        status=CapabilityStatus.AVAILABLE,
        phase=6,
        detail=(
            "Simulated orders, fills, fees, positions and PnL against real public "
            "market data. Every entry passes a risk gate that returns a named "
            "RISK_REJECTED_* code. Places NO real order: the paper engine has no venue "
            "path of any kind, and testnet execution is a separate service, route and "
            "switch that this engine cannot reach. "
            "Fills are all-or-nothing and management is poll-driven."
        ),
    ),
    Capability(
        key="paper.persistence",
        name="Paper state durability",
        status=CapabilityStatus.PARTIAL,
        phase=6,
        detail=(
            "PAPER STATE IS IN-MEMORY AND RESETS ON RESTART. Balances, positions, "
            "orders and history live in the server process only. The repository "
            "interface is in place so durable storage is a new implementation rather "
            "than a rewrite, but it needs the database from phase 1."
        ),
    ),
    Capability(
        key="paper.autonomous",
        name="Autonomous paper trading",
        status=CapabilityStatus.AVAILABLE,
        phase=7,
        detail=(
            "A server-side loop that opens and closes PAPER positions from strategy "
            "output without a human. OFF by default and on every restart: arming "
            "needs configuration, paper mode, and a deliberate call. Places NO real "
            "order. The watched universe is empty unless configured -- it never picks "
            "instruments on its own."
        ),
    ),
    Capability(
        key="risk.leverage_policy",
        name="Leverage request and approval architecture",
        status=CapabilityStatus.PARTIAL,
        phase=4,
        detail=(
            "The 1-500x request/approval chain exists and is tested, but nothing is "
            "ever approved in this build: the venue's per-symbol ceiling needs an "
            "authenticated endpoint and the risk engine is phase 7. Every request "
            "fails closed with a named reason. No leverage is set and no order is "
            "placed, in any mode."
        ),
    ),
    Capability(
        key="risk.engine",
        name="Risk engine",
        status=CapabilityStatus.AVAILABLE,
        phase=7,
        detail=(
            "Final authority over every proposed order: mode, emergency stop, daily "
            "locks, data freshness, cooldown, measured volatility, position slots, "
            "leverage, venue filters, exposure and balance. Refusals carry a named "
            "RISK_REJECTED_* code. It derives and reports a risk leverage ceiling, "
            "but leverage above 1x still fails closed because the venue's per-symbol "
            "maximum needs an authenticated endpoint this build does not have."
        ),
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
        status=CapabilityStatus.PARTIAL,
        phase=8,
        detail=(
            "Phase 8a: the order state machine, deterministic identity and the "
            "reconciliation protocol are built and tested. UNKNOWN can only be resolved "
            "through RECONCILING, and never by inference. Records are durable in "
            "PostgreSQL and a recovery pass runs at startup: orders that were in flight "
            "when the process stopped are moved to UNKNOWN, orders that never left are "
            "left alone, and orders that cannot be read are recorded as discrepancies "
            "and block new entries. Recovery **states the uncertainty; it does not "
            "resolve it** -- resolving needs venue evidence, which only a query against "
            "the venue supplies. The one venue these records reach is TESTNET, at "
            "demo-fapi.binance.com; NO adapter reports LIVE. Paper account state is "
            "separate and is still in-memory."
        ),
    ),
    Capability(
        key="order.persistence",
        name="Durable order records",
        status=CapabilityStatus.PARTIAL,
        phase=8,
        detail=(
            "Orders, fills and discrepancies are stored in PostgreSQL with NUMERIC(24,8) "
            "money and timestamptz time, behind row-level security. Identity is unique by "
            "database constraint, reconciliation holds a row lock for the whole "
            "read-decide-write, and a retry after a restart replays the persisted order "
            "instead of creating a second one. Applies to order records only: the paper "
            "account balance and positions are still in memory and say so. PARTIAL, not "
            "AVAILABLE: the submission-retry state a venue path needs "
            "(submission_attempts, next_retry_at) has columns but no writer. The testnet "
            "path submits without them, so a submission that fails is not automatically "
            "retried."
        ),
    ),
    Capability(
        key="execution.testnet",
        name="Testnet execution",
        status=CapabilityStatus.PARTIAL,
        phase=8,
        detail=(
            "Phase 8b: signed HMAC-SHA256 execution against the Binance USDT-M futures "
            "TESTNET only, at demo-fapi.binance.com, which is the single allowlisted "
            "host -- the production and superseded testnet hosts are refused by "
            "construction. One-way position mode only; a hedge-mode account is refused "
            "rather than adapted to. Every order is written down before it is sent, "
            "ruled on by the same risk engine the paper path uses, and submitted only "
            "after the venue CONFIRMS the approved leverage and ISOLATED margin -- a "
            "mismatch refuses. An unknown exchange ceiling refuses. Orders that vanish "
            "wholesale from the venue are treated as a reset condition needing human "
            "review, never converted into a guessed terminal state. PARTIAL: no "
            "autonomous testnet trading, no websocket fills, and completion requires an "
            "opt-in run against real testnet credentials."
        ),
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
