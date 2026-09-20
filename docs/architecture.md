# Aetheris Pro — Architecture

> Status: Phase 0 (foundation). This document describes the intended shape of
> the whole system and marks clearly which parts exist today. Anything not
> marked **implemented** is not built, and the running service reports the same
> thing at `GET /api/v1/system/capabilities`.

## 1. Design position

Aetheris Pro is an independently engineered trading platform. It takes its
*feature scope* from what mature trading systems are expected to do — market
data, strategies, backtesting, paper trading, risk control, execution — and
nothing else. No third-party trading engine's source, architecture, module
layout or naming is reproduced here.

Three convictions shape the code:

1. **The risk engine is the last word.** Everything upstream of it proposes;
   only it disposes.
2. **Absent data is a first-class value.** A missing price is represented, never
   substituted.
3. **Money is exact.** Binary floating point does not touch an account balance.

## 2. Layering

Dependencies point downward only. A lower layer never imports an upper one.

```
                 ┌─────────────────────────────────────┐
   delivery      │  api/ (FastAPI v1)   frontend/      │
                 └─────────────────────────────────────┘
                 ┌─────────────────────────────────────┐
   orchestration │  engines/ strategy · backtest ·     │
                 │           paper · order · portfolio │
                 └─────────────────────────────────────┘
                 ┌─────────────────────────────────────┐
   authority     │  engines/risk  ← final gate         │
                 └─────────────────────────────────────┘
                 ┌─────────────────────────────────────┐
   integration   │  adapters/ exchange · persistence   │
                 └─────────────────────────────────────┘
                 ┌─────────────────────────────────────┐
   foundation    │  core/  config · money · freshness  │
                 │         errors · logging · caps     │
                 │  domain/ enums · value objects      │
                 └─────────────────────────────────────┘
```

`core/` and `domain/` are pure: no I/O, no framework imports, no network. That
is what makes the indicator, risk and backtest layers testable without an
exchange.

## 3. The decision pipeline

```
market data → analysis → strategy → AI/ML → RISK ENGINE → execution
                                              │
                                              ├─ approve → order engine
                                              └─ reject  → RISK_REJECTED_* code
```

The risk engine sits on the only path to execution. A strategy cannot bypass
it, the AI layer cannot bypass it, Falcon cannot bypass it, and neither the
frontend nor an API client can bypass it — they all submit *proposals*.

Every rejection carries a machine-readable code from
`core.errors.RiskRejectionCode` (`RISK_REJECTED_DAILY_LOSS_LIMIT`,
`RISK_REJECTED_STALE_DATA`, …). Codes are stable: operators grep them, the UI
switches on them, tests assert on them.

**Implemented today:** the rejection vocabulary and the enum semantics.
**Not implemented:** the engine that issues them (phase 7).

## 4. Trading modes

`ANALYSIS → PAPER → TESTNET → LIVE`. No mode escalates into the next on its
own; each is switched on by a human.

| Mode | Real orders | Real funds | Default |
|---|---|---|---|
| ANALYSIS | no | no | always on |
| PAPER | no | no | on |
| TESTNET | **yes** | no | off |
| LIVE | **yes** | **yes** | off, double-gated |

`TESTNET` is deliberately classified as *placing real orders*. It reaches a
real matching engine and produces real order records; only the settlement
currency is worthless. Treating it as a simulation is how testnet bugs become
live bugs.

`LIVE` requires two independent switches — `live_trading_enabled` **and**
`live_activation_acknowledged`. A single environment variable is too easy to
inherit by copying someone else's config file; a second, differently named
affirmation makes enabling live trading a deliberate act. Startup fails loudly
if only one is set.

## 5. Data provenance

`core.freshness.Observation[T]` wraps every externally sourced value with its
source, exchange timestamp, receipt timestamp and status
(`OK` / `STALE` / `UNAVAILABLE` / `ERROR`).

A model validator enforces the invariant that makes this more than a
convention: **status `OK` requires a value, and any other status forbids
one.** It is therefore impossible to mark data stale and still hand a caller a
number they might read as current. Unknown data has no representation as a
plausible-looking number.

Only `DataStatus.OK` is usable for a trading decision.

## 6. Monetary arithmetic

`core.money` works in `Decimal` throughout and **rejects `float` at the type and
runtime level**. A caller holding a float from exchange JSON must pass
`str(value)`, which makes the lossy conversion explicit at the call site.

Exchange filters are truncation rules, not rounding rules:

- **Quantities** always truncate *down* to `stepSize`. Rounding a quantity up
  can spend funds that are not there; rounding down can at worst breach minimum
  notional, which the risk engine catches.
- **Prices** snap conservatively for their side — buys down, sells up — so
  quantisation never makes an order more aggressive than requested.

## 7. Honest capability disclosure

`core.capabilities` holds one entry per subsystem with a status of
`AVAILABLE` / `PARTIAL` / `UNAVAILABLE` / `PLANNED`, served at
`/api/v1/system/capabilities`.

The maintenance rule: **a capability becomes `AVAILABLE` only in the commit
that lands its tests, never ahead of them.** A test asserts that no trading
engine claims to be available while unbuilt, so drift between this registry and
reality breaks the suite.

`is_operational()` fails closed on an unknown key — a typo reads as "not
available", never as "available".

## 8. Observability

Structured logs only: events with fields, never sentences. Every request gets a
server-generated `X-Request-ID`; an inbound `X-Correlation-ID` is honoured so a
user action spanning services stays traceable, while the request ID remains
ours so a client cannot forge two requests into one identity.

Liveness and readiness are separate endpoints on purpose. A database outage
makes the service *unready*, not *dead*; conflating the two produces restart
loops that deepen an outage. Readiness distinguishes `NOT_CONFIGURED` (never
had it) from `UNAVAILABLE` (had it, lost it) — only the latter is an incident.

## 9. Current implementation status

| Area | Status |
|---|---|
| Configuration, mode gating, risk defaults | implemented, tested |
| Decimal money & exchange quantisation | implemented, tested |
| Data-freshness envelope | implemented, tested |
| Domain vocabulary (modes, order states, timeframes) | implemented, tested |
| Error envelope & risk-rejection codes | implemented, tested |
| Structured logging, request context, security headers | implemented, tested |
| Health, system status, capabilities API | implemented, tested |
| Authentication, database, migrations | **not started** (phase 1) |
| Exchange adapter, market data | **not started** (phase 2) |
| Scanner, indicators, SMC, strategies | **not started** (phases 3–4) |
| Backtesting, optimisation | **not started** (phase 5) |
| Paper engine | **not started** (phase 6) |
| Risk engine, portfolio | **not started** (phase 7) |
| Order engine, testnet, live | **not started** (phases 8, 10) |
| AI / Falcon | **not started** (phase 9) |
| Frontend | **not started** (phase 3) |

Nothing in this build places an order of any kind, in any mode.
