# Aetheris Pro

Professional crypto trading and quantitative research platform.

> **Phases 0, 2, 3, 4 and 5 of 10 — foundation, market data, scanner,
> terminal, indicators, strategy analysis and backtesting.**
> This build places **no orders in any mode**. It has no database, no
> strategies and no trading engines. What exists is the foundation
> (configuration, exact money arithmetic, data provenance, the error and risk
> vocabulary, observability) and a **read-only** Binance USDT-M Futures market-
> data layer: dynamic symbol discovery, tickers and OHLCV, each carrying
> provenance and a freshness status, plus a bounded market scanner with a
> deterministic — and fully published — Market Opportunity Score, eleven
> technical indicators, a rule-based strategy analysis layer, and a
> historical backtesting engine.
>
> Ask the running service what it can do: `GET /api/v1/system/capabilities`.

## What this is

Aetheris Pro is an independently engineered platform covering the scope a
serious trading system needs — market data, symbol discovery, indicators,
Smart Money Concepts analysis, strategies, backtesting, hyperparameter
optimisation, paper trading, risk management, portfolio accounting, order
execution, and an AI command layer called Falcon.

It is not a fork, port or reimplementation of any existing trading framework.
Feature *scope* is informed by what such systems are expected to do; the
architecture, domain model and code are its own.

## Principles

These are enforced by code and tests, not by convention:

1. **The risk engine has final authority.** Strategies, AI, Falcon, the
   frontend and API clients all merely *propose*. Every rejection carries a
   stable machine-readable code (`RISK_REJECTED_DAILY_LOSS_LIMIT`, …).
2. **Never fabricate data.** Market values travel in an `Observation` that
   holds either a value or a reason it has none — enforced by a validator, so
   "stale but here's a number anyway" is unrepresentable.
3. **Money is exact.** `Decimal` everywhere; `float` is rejected at the API of
   the money module. Quantities truncate down to step size, prices snap
   conservatively for their side.
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
8. **Leverage fails closed.** 1x–500x is the range analysis may *request*.
   Approval requires the venue's real per-symbol ceiling *and* the risk
   engine's — both unknown today, so nothing is ever approved. An unknown
   ceiling is never permission.
9. **A backtest is a simulation, not a forecast.** Signals fill at the next
   bar's open, intrabar ambiguity always resolves against the trade, and every
   result carries its assumptions, its warnings and what was not modelled.

## Trading defaults

| Setting | Default |
|---|---|
| Paper balance | 100 USDT |
| Daily profit target | +20 USDT (stops new entries, keeps managing positions) |
| Daily loss limit | −10 USDT (locks new entries, keeps managing positions) |
| Max leverage | 3× |
| Autonomous trading | **off** |
| Paper trading | available |
| Testnet trading | **off** |
| Live trading | **off** (double-gated) |

## API

All routes are `GET`. There is no non-GET route anywhere in this build, and a
test asserts it.

| Endpoint | Purpose |
|---|---|
| `/api/v1/health/live` · `/health/ready` | Liveness and readiness |
| `/api/v1/system/status` | Mode posture and risk defaults |
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
| `/api/v1/backtest/method` | Fill model, and what it does not model |
| `/api/v1/backtest/{symbol}` | Historical simulation over past candles |

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

Full instructions: [docs/setup.md](docs/setup.md).

## Layout

```
backend/           FastAPI service (Python 3.12+)
  src/aetheris/
    core/          config, money, freshness, errors, logging, capabilities
    domain/        enums and value objects (pure)
    api/v1/        HTTP surface
    adapters/      exchange + persistence integrations   (phase 1+)
    engines/       strategy, risk, backtest, paper, order (phase 4+)
  tests/           unit + integration
docs/              architecture, setup, ADRs
frontend/          Next.js terminal (read-only)
  src/app/         markets · scanner · backtest routes
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
- [Setup](docs/setup.md) — environment, commands, quality gates
- [ADRs](docs/adr/) — recorded decisions, including the open database question

## Quality gates

Every phase must pass all of these before it is called complete:

```bash
cd backend
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy

cd ../frontend
pnpm lint
pnpm typecheck
pnpm test
pnpm build
```

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | Repository and architecture foundation | **complete** |
| 1 | Authentication, database, configuration | blocked — see ADR 0002 |
| 2 | Exchange abstraction, Binance Futures market data | **complete** |
| 3 | Market terminal, scanner, frontend | **complete** |
| 4 | Indicators, strategy engine | **complete** (SMC deferred) |
| 5 | Backtesting engine | **complete** (optimisation deferred) |
| 6 | Paper trading engine | not started |
| 7 | Risk engine, portfolio | not started |
| 8 | Order execution, testnet | not started |
| 9 | AI/ML, Falcon | not started |
| 10 | Production hardening, live controls | not started |

## Security

Exchange credentials are server-side only and never reach the frontend, the
browser, source control or logs. Only trading permissions are ever required —
**withdrawal permission must never be granted to an Aetheris API key.**

## Disclaimer

Trading cryptocurrency derivatives carries substantial risk of loss. Backtest
and optimisation results are historical simulations and are not predictions.
No component of this system forecasts future prices or guarantees any outcome.
