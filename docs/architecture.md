# Aetheris Pro — Architecture

> Status: Phase 8a (order lifecycle). This
> document describes the intended shape of
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
   integration   │  adapters/exchange (Binance USDT-M)  │
                 │  adapters/ persistence  (phase 1)   │
                 └─────────────────────────────────────┘
                 ┌─────────────────────────────────────┐
   analysis      │  analysis/ metrics · scoring (pure) │
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

## 9. Exchange layer (phase 2)

`MarketDataPort` is the read-only contract every venue adapter implements;
`TradingPort` is implemented by `BinanceTestnetTradingAdapter` alone, which
reports `TESTNET` and reaches the Binance Demo venue only — an architecture
test asserts nothing reports `LIVE`. Binance-specific field names live in
exactly one module (`adapters/exchange/binance/parsing.py`) and never escape it
— an architecture test fails the build if a venue name or an exchange import
reaches `core/` or `domain/`.

Market data is dynamically discovered rather than configured: eligibility is a
pure predicate over venue metadata (`PERPETUAL`, `USDT`-quoted, `TRADING`,
positive tick and step), so newly listed contracts appear without a code
change. Against live Binance that is 905 instruments listed, 528 eligible.

Caches store domain objects, never `Observation` envelopes, and freshness is
recomputed on every serve — so a cached value can never be handed back wearing
a stale `OK`. Full detail in [exchange.md](exchange.md).

## 9b. Scanner layer (phase 3)

`analysis/` joins `core/` and `domain/` as a pure layer -- deterministic
arithmetic over candles with no I/O -- which is what makes it testable
without a network and reusable by the phase 5 backtester. The architecture
test enforces its purity alongside the others.

The scanner's design constraint is upstream cost: ticker fields come from one
whole-market request and can rank the full universe, while candle-derived
fields cost a request per instrument and are therefore bounded to a liquidity
pool. Every response declares which scope it used, so a subset is never
presented as the whole market. Full detail in [scanner.md](scanner.md).

## 9c. Analysis layer (phase 4)

`analysis/indicators/` and `analysis/strategies/` extend the pure layer. The
architecture test forbids them from importing HTTP, a framework, a venue **or
even settings** -- configuration is a deployment concern, not an arithmetic
one -- so `calculate_indicators(candles, keys, params)` and
`evaluate_from_candles(candles, ...)` are directly reusable by the phase 5
backtester over historical bars.

Indicator selection is a closed whitelist of registered callables and every
parameter is a typed, bounded field; no expression, formula string or `eval`
exists anywhere in the package.

The leverage chain (requested -> exchange ceiling -> risk ceiling -> approved)
lives here too, and fails closed on any unknown constraint. Detail in
[indicators.md](indicators.md) and [strategies.md](strategies.md).

## 9d. Backtesting (phase 5)

`analysis/backtest/` joins the pure layer and reuses phase 4 unchanged -- the
same indicator functions and the same rule set the live terminal shows, so no
separate backtest variant can drift from the strategy being analysed.

Indicators are computed once over the whole history rather than per prefix,
which is valid only because no indicator reads forward; a test recomputes the
bias on truncated prefixes and requires bar-for-bar agreement, so that
optimisation cannot silently become look-ahead.

Every ambiguity resolves against the trade: signals fill at the next bar's
open, and a bar covering both stop and target is assumed to have hit the stop.
Detail in [backtesting.md](backtesting.md).

## 9e. Paper trading (phase 6)

`engines/paper/` is the first layer that holds *state* rather than computing
over inputs, and the first that writes. Four modules, deliberately separate:

| Module | Responsibility |
|---|---|
| `store.py` | `PaperRepository` interface + an in-memory implementation |
| `state.py` | mutable working state, distinct from the frozen domain models |
| `risk.py` | **the gate** — every entry refusal, with its `RISK_REJECTED_*` code |
| `engine.py` | orders, fills, positions, management, PnL |

It is pure in the same sense the analysis layer is: no HTTP client, no adapter,
no framework, no venue name. `services/paper.py` is the only place it meets the
network. That is why a bug in simulated execution cannot become a real
order — the paper engine has no venue path at all; testnet execution is a
separate service, a separate route and a separate switch.

**The repository is the seam for durability.** Swapping the in-memory store for
a database-backed one is the whole of what persistent paper state requires; the
engine, service and API do not change. Until then, every response carries
`PAPER STATE: IN-MEMORY — RESETS ON RESTART`.

**Refusal ordering is part of the design.** Account-level refusals (mode,
emergency stop, daily lock) are checked before the order is sized, because a
size cannot be validated before it is computed and an incidental sizing problem
would otherwise mask a fundamental halt. Found running against live Binance
data; `check_authority` and three regression tests are the fix.

Detail in [paper-trading.md](paper-trading.md).

## 9g. Risk engine and autonomy (phase 7)

`engines/risk/` is the mode-independent authority: a pure function over a
policy, an account view and a market view, returning an approval with a size or
a refusal with a `RISK_REJECTED_*` code. Everything upstream proposes.

`services/autonomous.py` is the first component that acts without a human. It
is an `asyncio` task in the application process -- paper state is in-memory, so
a second process would trade a different account -- and it is **off by default**
with three independent conditions required to arm it.

Two properties are worth restating because they are easy to lose:

* **Two gates.** The risk engine rules first, the phase 6 paper gate re-checks
  independently. Neither trusts the other.
* **Building the authority did not unlock leverage.** `risk_max_leverage` is now
  derived and reported, but `exchange_max_leverage` still needs an authenticated
  endpoint, so the chain refuses above 1x exactly as before.

The loop depends on `PaperEngine` concretely. There is deliberately no
`ExecutionPort` a venue adapter could later satisfy, and no method takes a
`TradingMode` -- pointing autonomy at a venue requires new code, not new config.

Detail in [autonomous-trading.md](autonomous-trading.md).

## 9h. Order lifecycle (phase 8a)

`engines/order/` is the lifecycle an order has while this system is responsible
for it. Pure, like `engines/risk/`, and for the same reason: it drives a state
machine over a repository port and cannot reach a venue, so phase 8c attaching
an adapter is a deliberate act rather than something that could happen by
accident.

It exists for one scenario -- the window between a submission leaving and a
response arriving, in which local state cannot say whether an order exists. The
answer is structural rather than careful: ``UNKNOWN`` has exactly one outgoing
edge, ``RECONCILING``, so there is no path by which an unknown order becomes a
filled one without something having asked the venue.

Two other properties fall out of the same table. Submission requires validation,
so the risk check is a prerequisite rather than a call site. And terminal states
have no exits, so a late message about a settled order is recorded as evidence
instead of overwriting it.

``RISK_REJECTED_RECONCILIATION_PENDING`` -- defined in phase 0, unused until
now -- blocks every new entry while anything is unreconciled, and sits *before*
the daily lock because an unseen fill makes the day's realised PnL unreliable.

**Crash recovery is not delivered.** The protocol is built and tested; the
guarantee needs durable records, which is phase 8b on phase 1's database.

Detail in [order-engine.md](order-engine.md).

## 9f. The route surface, and how it changed

Phases 0–5 asserted that no route uses a method other than GET. Phase 6 makes
that false on purpose: submitting a paper order mutates server state, and a GET
that mutates is cacheable, prefetchable and repeatable by a browser doing
ordinary browser things.

The invariant narrowed rather than disappearing. Two assertions replace it:

1. **No route can reach a venue order endpoint.** No `TradingPort`
   implementation exists, and no venue order path is named in the package —
   asserted against the source, not just the served schema.
2. **Writes exist only under `/paper`**, against in-memory simulation state.
   No PUT, PATCH or DELETE exists anywhere, and CORS advertises exactly the two
   verbs that do.

## 9a. Current implementation status

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
| Exchange abstraction (`MarketDataPort`) | implemented, tested |
| Binance USDT-M public market data (read-only) | implemented, tested |
| Dynamic symbol discovery and search | implemented, tested |
| Ticker and OHLCV with provenance | implemented, tested |
| Bounded TTL cache, retry/backoff, rate-limit handling | implemented, tested |
| Market scanner (bounded search/filter/sort/paging) | implemented, tested |
| Scanner candle statistics + opportunity score | implemented, tested |
| Indicator engine (11 indicators, hand-verified) | implemented, tested |
| Strategy analysis engine + registry | implemented, tested |
| Leverage request/approval architecture | implemented, always fails closed |
| Smart Money Concepts | **not started** |
| Websockets, funding rate, open interest | **not started** |
| Backtesting engine (fills, fees, slippage, drawdown) | implemented, tested |
| Hyperparameter optimisation | **not started** |
| Paper trading engine (orders, fills, positions, PnL) | implemented, tested |
| Paper entry risk gate (`RISK_REJECTED_*` refusals) | implemented, tested |
| Paper state persistence | **in-memory only**, resets on restart (phase 1) |
| Autonomous paper trading (off by default) | implemented, tested |
| Risk engine (final authority over every proposal) | implemented, tested |
| Portfolio accounting | **not started** (phase 7+) |
| Order lifecycle, identity, reconciliation protocol | implemented, tested |
| Durable order records / crash recovery | **not started** (phase 8b) |
| Testnet, live execution | **not started** (phases 8c, 10) |
| AI / Falcon | **not started** (phase 9) |
| Frontend terminal (markets, scanner, backtest, paper) | implemented, tested |

Nothing in this build sends an order to any exchange, in any mode. Paper
orders are records in the server process, filled by arithmetic against real
observed prices. No API credential exists anywhere in the system.
