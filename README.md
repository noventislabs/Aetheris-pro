# Aetheris Pro

Professional crypto trading and quantitative research platform.

> **Phases 0 and 2–8b of 10.** Foundation, market data, scanner, terminal,
> indicators, strategy analysis, backtesting, paper trading, the risk engine,
> autonomous paper trading, the durable order lifecycle, PostgreSQL order
> persistence, crash recovery, and **signed execution against the Binance
> USDT-M Futures Demo (testnet) venue**.
>
> **This build can place a real order — on the Binance Demo venue only**, at
> the single allowlisted host `demo-fapi.binance.com`, and only when testnet
> trading is switched on and credentials are supplied. The production host is
> refused by construction. **Live trading does not exist**: no adapter reports
> `LIVE`, and the switch is double-gated and off.
>
> **Paper account state is still in-memory and resets on restart** — said on
> every response, not buried here. Durable storage covers *order records*, not
> the paper balance.
>
> Ask the running service what it can do: `GET /api/v1/system/capabilities`.

## What this is

Aetheris Pro is an independently engineered platform covering the scope a
serious trading system needs — market data, symbol discovery, indicators,
strategies, backtesting, paper trading, risk management, a durable order
lifecycle, and venue execution.

It is not a fork, port or reimplementation of any existing trading framework.
Feature *scope* is informed by what such systems are expected to do; the
architecture, domain model and code are its own.

Smart Money Concepts, portfolio accounting and the Falcon AI command layer are
**scope, not code** — see [Planned and locked](#planned-and-locked).

## Pipeline

```
Market data            Binance USDT-M Futures, public REST, read-only
      ↓
Analysis               scanner · 11 indicators · rule-based strategy
      ↓
Market regime          TREND_UP / TREND_DOWN / RANGE / volatility / UNKNOWN
      ↓
Setup score            0-100 strategy alignment, with risk/reward
      ↓
Backtesting            historical simulation over past candles
      ↓
Paper                  simulated fills at real observed prices
      ↓
Risk engine            THE AUTHORITY — every order, whatever proposed it
      ↓
Order lifecycle        12 states; UNKNOWN is never guessed
      ↓
PostgreSQL             durable order records, fills, discrepancies
      ↓
Binance Demo/Testnet   signed HMAC-SHA256 execution
      ↓
LIVE                   LOCKED — not implemented, double-gated, no adapter
```

## Principles

These are enforced by code and tests, not by convention:

1. **The risk engine has final authority.** Strategies, the autonomous loop,
   the frontend and API clients all merely *propose*. Manual and autonomous
   paper orders both pass through `engines.risk.evaluate`, and there is no
   second leverage resolution and no path around it (ADR 0006). The testnet
   path is ruled on by the same engine. Every rejection carries a stable
   machine-readable code (`RISK_REJECTED_DAILY_LOSS_LIMIT`, …).
2. **Never fabricate data.** Market values travel in an `Observation` that
   holds either a value or a reason it has none — enforced by a validator, so
   "stale but here's a number anyway" is unrepresentable. There is no fake
   market data, no fake signal, no fake confidence and no fake execution
   anywhere in the system.
3. **Money is exact.** `Decimal` everywhere; `float` is rejected at the API of
   the money module. Quantities truncate down to step size, prices snap
   conservatively for their side. Persisted money is `NUMERIC(24,8)`, persisted
   time is `timestamptz`.
4. **Modes never escalate themselves.** `ANALYSIS → PAPER → TESTNET → LIVE`,
   each enabled by a human. Live needs two independent switches.
5. **Claim nothing unbuilt.** The capability registry is served over the API,
   and a test fails if an unimplemented engine claims to be available.
6. **Ranking is not prediction.** The Market Opportunity Score is a
   deterministic ranking metric over present-tense measurements — never a
   probability of profit, expected return, win rate or forecast. Its formula
   and weights are published at `/api/v1/scanner/scoring-method`.
7. **Analysis is not an instruction.** A strategy reports which named
   conditions hold right now, with the number each one measured. There is no
   confidence, probability or prediction anywhere in it.
8. **Leverage fails closed.** `approved = min(requested, exchange_max,
   risk_max)`, and an unknown link refuses rather than assumes. The two paths
   differ because their evidence differs, not because their rules do — see
   [Leverage](#leverage).
9. **A backtest is a simulation, not a forecast.** Signals fill at the next
   bar's open, intrabar ambiguity always resolves against the trade, and every
   result carries its assumptions, its warnings and what was not modelled.
10. **Paper trading is not testnet trading.** A paper order is arithmetic in
    the server process and reaches no venue. A testnet order is signed and sent
    to Binance Demo. They are separate routes, separate state and separate
    switches; the `/testnet` page reads no paper state, and a test asserts the
    paper balance never appears on it.
11. **Approval is binding, not advisory.** Before a testnet order is sent, the
    venue must *confirm* the approved leverage and ISOLATED margin. A mismatch
    refuses the order rather than adapting to it.

## Implemented

### Market data and analysis
- **Binance USDT-M Futures market data** — public REST, read-only, no
  credentials on this path.
- **Dynamic USDT-M symbol discovery** — the universe is read from the venue,
  not hardcoded, with per-instrument filters.
- **Market scanner** — bounded search, filter, sort and pagination, with a
  deterministic and fully published Market Opportunity Score. Strategy setup
  scoring is available as an opt-in enrichment and a sort key; it reaches a
  smaller pool than metrics do, because every indicator has to warm up, and
  the page reports `STRATEGY_POOL` with that pool's size rather than letting
  a slice read as the whole market. Both scores travel separately and are
  never merged.
- **11 technical indicators** — SMA, EMA, Bollinger Bands, Rolling VWAP, RSI,
  MACD, Stochastic Oscillator, ATR, ADX, ROC, CCI. No look-ahead; warm-up
  rules are explicit.
- **Rule-based strategy analysis** — a trend/momentum rule set reporting which
  named conditions hold, with the measured value of each.
- **Market regime classification** — `TREND_UP`, `TREND_DOWN`, `RANGE`,
  `HIGH_VOLATILITY`, `LOW_VOLATILITY` or `UNKNOWN`, from ADX, an EMA pair and
  ATR percentage. Thresholds are published and versioned; the volatility axis
  is reported separately so a trending market still says whether it is calm or
  violent. `UNKNOWN` is never collapsed into `RANGE`. **Arithmetic, not a
  model** — the learned classifier in phase 9 remains unbuilt.
- **Setup scoring** — a bounded 0-100 score over six published, weighted
  components: trend and momentum alignment, independent regime agreement,
  risk/reward, volatility fitness and volume confirmation. Deterministic, and
  every component carries its raw measurement so the total can be recomputed
  by hand. Long and short are evaluated independently. It measures **strategy
  alignment, not a probability of profit** — see [Scores](#scores).
- **Risk/reward derivation** — real entry, stop and target from fixed-percent,
  ATR or structure stop models at a configurable R multiple. A setup whose
  stop cannot be derived safely reports `NO_ACTIONABLE_SETUP` rather than
  inventing a level.
- **Backtesting** — historical simulation with a pessimistic fill model that
  publishes its assumptions and what it does not model. Reports win rate,
  profit factor, drawdown, longest losing streak, average R and exposure.
- **Bounded parameter search** — deterministic grid search over real strategy
  parameters against a published multi-factor objective, with chronological
  train/validation/test windows enforced by the split type itself. Selection
  reads validation; the test window is scored once afterwards and never
  influences the choice.

### Trading
- **Risk engine** — ordered checks covering mode, emergency stop,
  reconciliation state, the daily loss lock, data freshness, cooldown,
  volatility, position slots, leverage and sizing. The single authority for
  every order on both the paper and testnet paths.
- **Paper trading** — simulated fills against real observed bid/ask, fees
  charged per side, realised and unrealised PnL, stop-loss, take-profit,
  trailing stop, liquidation and signal-flip closes.
- **Autonomous paper trading** — an in-process loop that proposes orders and is
  refused by the risk engine exactly as a human is. **Off by default, disarmed
  on every restart, and paper-only** — it has no code path to the testnet venue.
- **Durable order lifecycle** — 12 states (`CREATED`, `VALIDATING`,
  `SUBMITTED`, `ACCEPTED`, `PARTIALLY_FILLED`, `FILLED`, `CANCEL_REQUESTED`,
  `CANCELLED`, `REJECTED`, `EXPIRED`, `UNKNOWN`, `RECONCILING`). `UNKNOWN` is
  resolved only through `RECONCILING`, never by inference.
- **PostgreSQL order persistence** — `orders`, `order_fills`,
  `order_discrepancies`, `accounts` and `users` tables, `NUMERIC(24,8)` money,
  `timestamptz` time, row-level security, and four Alembic migrations
  (`0001`–`0004`). Identity is unique by database constraint.
- **Crash/recovery primitives** — a recovery pass runs at startup: orders in
  flight when the process stopped become `UNKNOWN`, orders that never left are
  left alone, and unreadable orders are recorded as discrepancies that block
  new entries. Recovery **states the uncertainty; it does not resolve it.**
- **Idempotency and replay protection** — `client_order_id` is derived
  deterministically (`blake2b` over account and intent key), enforced unique in
  the database, and a retry after a restart replays the persisted order instead
  of creating a second one.
- **Reconciliation** — `decide`, `needs_reconciliation`, `blocking_orders`,
  `unreconciled_count`, with actions `ADOPT_VENUE_STATE`,
  `RESOLVE_NEVER_SUBMITTED`, `REMAIN_UNKNOWN`, `RECORD_DISCREPANCY` and
  `VENUE_UNREACHABLE`. Unreconciled orders block new entries through the risk
  engine's `RECONCILIATION_PENDING` check, which fires from real durable
  records.

### Testnet execution
- **Binance Demo execution** — signed HMAC-SHA256 requests against
  `demo-fapi.binance.com`, the single allowlisted host. The production host and
  the superseded `testnet.binancefuture.com` host are both rejected by a
  validator; the base URL must be `https` and carry no path.
- **One-way position mode only** — a hedge-mode account is refused, not adapted
  to.
- **Leverage validation, set and verification** — the per-symbol bracket is
  read for the tier the order's notional actually falls in, leverage is set,
  and the venue's applied value is read back and compared. A mismatch refuses.
- **Isolated margin verification** — ISOLATED is set and then read back and
  confirmed before submission. A mismatch refuses.
- **Write-before-send** — the order is committed as `SUBMITTED` *before* the
  network call, so a crash in the window leaves the exact signature recovery
  reads.
- **Testnet API** — `GET /api/v1/testnet/status`,
  `POST /api/v1/testnet/orders`, `POST /api/v1/testnet/orders/{order_id}/cancel`.
- **Testnet UI** — a `/testnet` page showing balance, trade permission,
  position mode, margin mode, positions and open orders with cancel, behind a
  permanent `TESTNET • BINANCE DEMO` badge and a no-real-funds banner. It is
  linked in navigation only when the backend reports testnet enabled.

## Limitations

These are real constraints of the current build, not opinions about it:

- **Paper account state is in-memory.** Balance, positions, open paper orders,
  the order log, trade history, the daily session and the emergency-stop flag
  are held by `InMemoryPaperRepository` and are lost on restart. Durable
  storage covers order records only.
- **Autonomy arm state is non-durable by design.** The loop starts disarmed on
  every process start, however it was left.
- **REST polling only. There is no WebSocket anywhere** — not for market data,
  not for fills. The frontend polls on a 10s interval that pauses on hidden
  tabs.
- **Order durability requires configuration.** Without `DATABASE_URL` the order
  lifecycle is simply absent, readiness reports `NOT_CONFIGURED`, and nothing
  claims crash recovery. There is deliberately no in-memory fallback, because a
  development store would make the application look healthy while the one thing
  persistence exists for silently did not happen.
- **Paper leverage is effectively 1x.** The paper path reads its ceiling from
  public venue metadata, which carries no leverage brackets, so anything above
  1x refuses with `EXCHANGE_MAX_UNKNOWN`. See [Leverage](#leverage).
- **Autonomous trading is paper-only.** There is no autonomous testnet trading.
- **Testnet fills are not streamed.** Order state is read back by query, not
  pushed.
- **Market orders only** in the paper engine; limit orders and partial fills
  are not implemented there.
- **Paper position management is poll-driven**, via `POST /api/v1/paper/tick`
  and the autonomous loop, not continuous.

## Planned and locked

Nothing below is implemented. None of it is reachable at runtime.

| Capability | Status |
|---|---|
| **Live trading** | **LOCKED.** No adapter reports `LIVE`. Double-gated behind `live_trading_enabled` *and* `live_activation_acknowledged`, both off. |
| **Falcon AI command centre** | Planned, phase 9. No module. |
| **AI/ML analysis layer** | Planned, phase 9. No model is bundled. |
| **Smart Money Concepts** | Planned. Not implemented — no SMC module exists. |
| **Walk-forward optimisation** | Partial. Rolling windows are implemented and tested as a primitive, but the optimizer performs a single three-way split and does not yet drive them. No API route and no stored reports. |
| **WebSocket streaming** | Not implemented, for market data or fills. |
| **Authentication and users** | Planned, phase 1. The `users` table exists; Argon2id credentials and sessions do not. |
| **Portfolio and position accounting** | Planned. |
| **Durable paper state** | Planned. The repository is the seam. |

## Leverage

`approved = min(requested, exchange_max, risk_max)` — and an unknown link
refuses. The rule is identical on both paths; only the available evidence
differs.

| | Paper | Testnet |
|---|---|---|
| `exchange_max` | **unknown** — public metadata carries no brackets | **known** — read from the authenticated bracket endpoint, for the order's notional tier |
| `risk_max` | derived by the risk engine | derived by the risk engine |
| Above 1x | refused, `EXCHANGE_MAX_UNKNOWN` | approvable, then **verified at the venue** before submission |

An unknown ceiling is never permission. On the testnet path the approval is
binding: the venue must confirm both the leverage and ISOLATED margin, or the
order refuses.

## Scores

Three different numbers in this system are easy to confuse, so they are named
and bounded differently on purpose.

| | What it measures | What it is not |
|---|---|---|
| **Setup score** (0–100) | How completely current measurements satisfy one strategy's published rules, and whether the resulting trade is worth its own risk. | Not a probability of profit, a win rate, or an expected return. |
| **Market Opportunity Score** (scanner) | How unusually active an instrument is right now, for ranking. | Not a prediction, and not a claim that a trade exists. |
| **Backtest metrics** | What a rule set actually did over specific historical bars under stated assumptions. | Not a forecast. A historical win rate is not a future probability. |

**Setup score and backtest metrics are never merged.** A setup scoring 90 does
not have a 90% win rate, and the two are reported as separate objects so no
caller can accidentally present one as the other. Historical performance is
deliberately excluded from the setup score's components for the same reason —
folding it in is what turns a present-tense alignment measure into something
that reads like a forecast.

No component of this system produces a calibrated probability of profit,
because nothing in it is a model that could.

## Trading defaults

| Setting | Default |
|---|---|
| Paper balance | 100 USDT |
| Daily profit target | +20 USDT (stops new entries, keeps managing positions) |
| Daily loss limit | −10 USDT (locks new entries, keeps managing positions) |
| Max leverage | 3× |
| Max open positions | 5 |
| Autonomous trading | **off** |
| Paper trading | available |
| Testnet trading | **off** |
| Live trading | **off** (double-gated) |

## API

Reads are `GET`. Writes exist only under `/paper` and `/testnet`. There is no
PUT, PATCH or DELETE anywhere, and tests assert it.

| Endpoint | Purpose |
|---|---|
| `/api/v1/health/live` · `/health/ready` | Liveness and readiness |
| `/api/v1/system/status` | Mode posture of **this app instance** |
| `/api/v1/system/capabilities` | What is genuinely implemented |
| `/api/v1/markets/status` | Observed exchange connectivity |
| `/api/v1/markets/exchange-info` | Venue metadata and instrument counts |
| `/api/v1/markets/symbols` | Discovered universe, with `search` |
| `/api/v1/markets/symbols/{symbol}` | One instrument's metadata and filters |
| `/api/v1/markets/{symbol}/ticker` | Ticker in an observation envelope |
| `/api/v1/markets/{symbol}/klines` | Candles in an observation envelope |
| `/api/v1/scanner` | Bounded scan: search, filter, sort, paginate |
| `/api/v1/scanner/scoring-method` | How the opportunity score is calculated |
| `/api/v1/analysis/indicators` | Indicator catalogue, with each convention |
| `/api/v1/analysis/strategies` | Registered strategies and their rules |
| `/api/v1/analysis/{symbol}/indicators` | Calculate a bounded indicator set |
| `/api/v1/analysis/{symbol}/strategy` | Rule-based bias — analysis only |
| `/api/v1/analysis/{symbol}/setup` | Scored setup: direction, components, regime, levels |
| `/api/v1/backtest/method` | Fill model, and what it does not model |
| `/api/v1/backtest/{symbol}` | Historical simulation over past candles |
| `/api/v1/paper/method` | Paper fill model, gaps, refusal vocabulary |
| `/api/v1/paper/account` | Balance, equity, positions, day session |
| `/api/v1/paper/reconciliation` | Reconciliation posture for the paper account |
| `POST /api/v1/paper/orders` | Submit a **simulated** order |
| `POST /api/v1/paper/tick` | One position-management pass |
| `POST /api/v1/paper/positions/{symbol}/close` | Close at the observed price |
| `POST /api/v1/paper/emergency-stop` | Block or unblock new entries |
| `POST /api/v1/paper/reset` | Discard the paper account |
| `/api/v1/paper/autonomous` | Loop state — off unless armed |
| `/api/v1/paper/autonomous/decisions` | The audit trail, including idle iterations |
| `POST /api/v1/paper/autonomous` | Arm or disarm the loop |
| `/api/v1/testnet/status` | Venue status, account and open state — `DISABLED` when off |
| `POST /api/v1/testnet/orders` | Submit a **real Binance Demo order** through the risk engine |
| `POST /api/v1/testnet/orders/{order_id}/cancel` | Request cancellation at the venue |

See [docs/exchange.md](docs/exchange.md) for the exchange layer in detail.

## Quick start

```bash
# backend
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m uvicorn aetheris.main:app --reload

# terminal (second shell)
cd frontend
pnpm install
pnpm dev
```

API docs at http://127.0.0.1:8000/docs, terminal at http://localhost:3000

The service starts without a database and without credentials; the order
lifecycle and testnet execution are simply absent and say so. To enable durable
orders set `DATABASE_URL` and run `alembic upgrade head`. To enable testnet
execution supply Binance **Demo** credentials and switch it on.

Full instructions: [docs/setup.md](docs/setup.md).

## Layout

```
backend/           FastAPI service (Python 3.12+)
  src/aetheris/
    core/          config, money, freshness, errors, logging, capabilities
    domain/        enums and value objects (pure)
    api/v1/        HTTP surface
    adapters/
      exchange/    market data (public) + Binance Demo trading (signed)
      persistence/ engine, models, order and account repositories
    engines/       paper (6) · risk (7) · order lifecycle (8a)
    services/      the only layer that touches the network
  migrations/      Alembic revisions 0001–0004
  tests/           unit + integration
docs/              architecture, setup, ADRs
frontend/          Next.js terminal
  src/app/         markets · scanner · backtest · paper · testnet routes
  src/components/  chart, table, search, data states
  src/lib/         typed API client, formatting, polling hook
```

## Documentation

- [Architecture](docs/architecture.md) — layering, decision pipeline, modes,
  provenance, current status
- [Exchange layer](docs/exchange.md) — adapter design, symbol discovery,
  freshness, caching, rate limiting, error codes
- [Scanner](docs/scanner.md) — bounded scanning, candle statistics, the
  Market Opportunity Score and what it is not
- [Terminal](docs/terminal.md) — frontend stack, data states, responsive
  behaviour, API contract safety
- [Indicators](docs/indicators.md) — formulas, conventions, warm-up rules,
  the no-look-ahead design
- [Strategies](docs/strategies.md) — the rule set, the freshness gate, and
  the leverage request/approval architecture
- [Backtesting](docs/backtesting.md) — the fill model, its pessimism rules,
  the metrics and what is deliberately not simulated
- [Paper trading](docs/paper-trading.md) — the risk gate, the fill model,
  paper vs testnet vs live
- [Risk engine and autonomy](docs/autonomous-trading.md) — final authority,
  the leverage ceiling derivation, the loop, and why it is off by default
- [Order engine](docs/order-engine.md) — the state machine, deterministic
  identity, and why `UNKNOWN` can only be resolved by asking
- [Phase 1 database requirements](docs/phase-1-database-requirements.md)
- [Setup](docs/setup.md) — environment, commands, quality gates
- [ADRs](docs/adr/) — recorded decisions

## Quality gates

Every phase must pass all of these before it is called complete. Run them
yourself; this README does not assert a result it has not shown you.

```bash
cd backend
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy
.venv/Scripts/python.exe -m alembic check

cd ../frontend
pnpm lint
pnpm typecheck
pnpm test
pnpm build
```

Testnet execution has an additional opt-in suite that runs against real Binance
Demo credentials. It is skipped by default.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | Repository and architecture foundation | **complete** |
| 1 | Authentication, database, configuration | **partial** — PostgreSQL, migrations and tenant isolation delivered; authentication and users not started |
| 2 | Exchange abstraction, Binance Futures market data | **complete** |
| 3 | Market terminal, scanner, frontend | **complete** |
| 4 | Indicators, strategy engine | **complete** (SMC deferred) |
| 5 | Backtesting engine | **complete** (optimisation deferred) |
| 6 | Paper trading engine | **complete** (state in-memory) |
| 7 | Risk engine, autonomous paper trading | **complete** (portfolio accounting deferred) |
| 8a | Order lifecycle state machine | **complete** |
| 8b | Durable orders, crash recovery, testnet execution and UI | **complete** |
| 9 | AI/ML, Falcon | not started |
| 10 | Production hardening, live controls | not started |

## Security

- Exchange credentials are **server-side only**. They never reach the frontend,
  the browser, source control or logs.
- **The frontend never receives a Binance API credential and never signs a
  Binance request.** Signing lives in exactly one backend module
  (`adapters/exchange/binance/signing.py`). No credential name, signature or
  `X-MBX-APIKEY` header appears anywhere in the frontend source.
- **Testnet uses the Binance Demo venue only.** `demo-fapi.binance.com` is the
  single allowlisted host, stated as an allowlist rather than a denylist so a
  new production hostname cannot slip past.
- **Production Binance execution is not enabled.** No adapter reports `LIVE`.
- Only trading permissions are ever required — **withdrawal permission must
  never be granted to an Aetheris API key.**
- Secrets are held in `SecretStr` and are never logged; `.env` is gitignored
  and untracked.

## Disclaimer

Trading cryptocurrency derivatives carries substantial risk of loss. Backtest
results are historical simulations and are not predictions. No component of
this system forecasts future prices or guarantees any outcome. Testnet orders
are placed on the Binance Demo venue with simulated funds.
