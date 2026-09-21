# Paper trading

> Status: Phase 6. **Simulation only.** No order reaches any exchange, no API
> credential exists anywhere in this system, and no real funds are involved.
> Paper trading is not testnet trading and not live trading — section 2 spells
> out the difference.

## 1. What it actually is

A paper order is a record in the server process. A paper fill is arithmetic
against a price Binance really published, a second or two ago. That is the
whole mechanism.

It is safe for a structural reason rather than a careful one: there is no
implementation of `TradingPort` anywhere in the codebase, no Binance order path
is named in any source file, and the engine cannot import a transport, a
framework or a venue adapter — an architecture test fails the build if it tries.
A bug in simulated execution has nowhere to send an order to.

## 2. Paper vs testnet vs live

Routinely conflated, and only one of the three is harmless.

| | Orders sent | Credentials | Money | Built |
|---|---|---|---|---|
| **Paper** (this) | none, anywhere | none | none | yes |
| **Testnet** | real orders to a venue's test environment | testnet API keys | test funds | no — phase 8 |
| **Live** | real orders to the live venue | live API keys | real | no — phase 10 |

Testnet is *not* a safer paper mode. It places real orders against a real
matching engine and needs a real credential; it simply settles in play money.
That is why it is a later phase behind its own switch, not a variation of this
one. Live needs two independent switches and is off by default.

Whatever mode is eventually enabled, **withdrawal permissions are never
required**, and no credential is ever stored in the frontend, in browser
storage, in source, in Git, in logs or in a URL.

## 3. The pipeline

Fixed, one-directional, and the gate sits in the middle of it:

```
market data → fill price → size against venue filters → RISK GATE
            → order → position → management → close → PnL
```

**The risk gate is the only door.** A human clicking a button, a strategy, and
an API client all arrive at the same function with the same authority, which is
none. Nothing downstream can overrule a refusal.

### Refusal ordering

Refusals are ranked by how fundamental they are, and the *first* thing wrong is
what gets reported:

1. `MODE_NOT_ENABLED` — paper trading is off in this deployment
2. `EMERGENCY_STOP` — the desk is halted
3. `DAILY_PROFIT_TARGET` / `DAILY_LOSS_LIMIT` — the day's budget is spent
4. `STALE_DATA` — no usable price
5. `MAX_LEVERAGE` — the constraint chain approved nothing
6. sizing, venue filters, balance, exposure

Steps 1–3 are checked *before* the order is sized, and that ordering was not
free. Running against live data, an emergency stop plus an unsizeable BTCUSDT
order reported `EXCHANGE_PRECISION` — an incidental detail — while the stop went
unmentioned. A size cannot be validated before it is computed, so the
account-level refusals had to move ahead of sizing. `check_authority` exists for
exactly that, and three regression tests pin it.

## 4. Prices are never invented

A fill uses the venue's **best ask** for a buy and **best bid** for a sell, so
the spread is a measured cost rather than a modelled one. Only when the venue
publishes no book does slippage become an assumption, and the fill records which
of the two happened:

```
"price_source": "binance-futures-usdm:rest:best_ask"
"price_source": "binance-futures-usdm:rest:last_price+2bps_slippage"
```

An order is **refused outright** when market data is unavailable, stale, or of
unverifiable age. There is no last-known-price fallback and no substituted zero.
The same applies to closing: closing at an invented price would book a
fabricated PnL into the balance, which is worse than leaving a position open and
saying why.

A position that could not be marked reports `mark_price: null` and
`unrealized_pnl: null`. Not zero — zero would assert the position is flat, which
is a claim no observation made.

## 5. Leverage: 1x is what you get, and here is why

Paper does **not** get a softer constraint chain than live would. The Phase 4
chain runs unchanged:

```
approved = min(requested, exchange_max, risk_max)   — only if all are known
```

Today `exchange_max` is unknown (leverage brackets come from an authenticated
endpoint; this build holds no credentials) and `risk_max` is unknown (the risk
engine is phase 7). So **anything above 1x is refused**, with the exact reason
on the response:

```json
{"requested_leverage": "10", "exchange_max_leverage": null,
 "risk_max_leverage": null, "approved_leverage": null,
 "reason": "RISK_REJECTED_EXCHANGE_MAX_LEVERAGE_UNKNOWN"}
```

That is the fail-closed architecture working against real requests, before it
ever guards real money.

**1x is the one exception, and it is arithmetic rather than a loophole.** Every
ceiling this domain accepts is at least 1x, so `min(1, any valid ceiling)` is 1
whatever the missing values turn out to be — learning them could not change the
answer. And 1x is unlevered: margin equals notional, nothing is borrowed, and
the liquidation the chain exists to prevent has no mechanism. It carries its own
reason code, `LEVERAGE_APPROVED_AT_DOMAIN_MINIMUM`, so it is never mistaken for
an approval the full chain produced.

To explore leveraged outcomes, use the [backtest engine](backtesting.md), where
leverage is an explicit simulation input capped at 25x that authorises nothing.

## 6. Accounting

**`balance == starting_balance + realized_pnl`, always.** Entry fees are
realised the moment they are paid, so they move balance and realised PnL
together; the closing trade's `net_pnl` already includes them. Double-charging
the entry fee is the easy mistake here, and an invariant test walks a whole
round trip checking this equation at every step.

- **Margin is reserved, not spent.** `available_balance = balance − margin_used`.
- **Unrealised profit does not fund new positions.** Real cross margin allows
  it; refusing to is the conservative direction, and a paper engine stricter
  than the venue never flatters a strategy.
- **Quantity always truncates down** to the venue's step size. Rounding up can
  spend margin that is not there.
- **Return % is measured against margin**, not notional — the money actually at
  risk.

### Fees

5 bps per side on notional, matching the backtest default so the two
simulations are comparable. Configurable via `AETHERIS_PAPER_TAKER_FEE_BPS`.

## 7. Position management is poll-driven

There is **no server-side loop**. Stops, targets, trailing stops and liquidation
are evaluated when `POST /paper/tick` is called, which the terminal does every
5 seconds while the page is open. Close the tab and nothing is evaluated until
you return.

Three consequences, stated rather than discovered:

- A level is noticed on a poll, not the instant it is touched, so the fill uses
  the price observed **then** — which can be past the level. **A realised loss
  can exceed the stop distance.** Real stop-market orders slip too, so this errs
  toward reality rather than away from it.
- When one poll crosses several adverse levels at once, the one **nearest entry**
  fires: that is the one price must have passed first.
- An adverse level always beats a target. Assuming the favourable one is the
  single most common way a simulation flatters itself.

`GET /paper/account` marks to market and changes nothing, so a browser
refreshing a tab can never realise a loss.

## 8. Daily limits

Defaults: target **+20 USDT**, limit **−10 USDT**, both on a UTC day.

Evaluated on **realised** PnL. Using unrealised would let an open position's
fluctuation toggle the lock tick by tick, and a brake that engages and releases
on noise is not a limit.

A lock stops **opening**. It never closes an open position — force-closing on a
daily limit is a separate policy decision, and doing it implicitly would turn a
risk brake into a market order nobody asked for. The same is true of the
emergency stop: it halts entries and nothing else, because an emergency switch
that fires market orders is its own way to lose money badly.

Locks reset at the UTC day boundary. A lock that survived midnight would be a
permanent stop wearing a daily name.

## 9. Durability

**PAPER STATE: IN-MEMORY — RESETS ON RESTART.**

Balances, positions, orders and history exist in the server process only. This
is on every account response as `durability_notice`, on the terminal as a
permanent banner, and in the capability registry as
`paper.persistence: PARTIAL` — a balance that silently resets is worse than one
the user knows resets.

The `PaperRepository` interface exists now so durable storage is a new
implementation rather than a rewrite: the engine, service and API do not change.
It needs the database from phase 1.

`GET /paper/reconciliation` reports `NOT_APPLICABLE` rather than `CONSISTENT`.
There is no external authority to disagree with, and claiming consistency would
imply a comparison that never happened.

### One account, deliberately

There is exactly one server-side paper account and **no request can name one**.
That is what keeps this free of an IDOR while authentication does not exist:
a caller cannot ask for someone else's account because there is no identifier to
ask with. Multi-tenant isolation needs phase 1.

## 10. API

| Route | Method | Effect |
|---|---|---|
| `/api/v1/paper/method` | GET | fill model, gaps, refusal vocabulary |
| `/api/v1/paper/account` | GET | balance, equity, positions, session |
| `/api/v1/paper/reconciliation` | GET | `NOT_APPLICABLE` |
| `/api/v1/paper/orders` | POST | submit a simulated order |
| `/api/v1/paper/tick` | POST | one management pass |
| `/api/v1/paper/positions/{symbol}/close` | POST | close at the observed price |
| `/api/v1/paper/emergency-stop` | POST | block/unblock entries |
| `/api/v1/paper/reset` | POST | discard the account |

**These are the first non-GET routes in the system.** Phases 0–5 asserted a flat
invariant — no route uses a method other than GET — which was worth asserting
while nothing had state to mutate. Submitting a paper order genuinely mutates
server state, and modelling that as a GET would make a state change cacheable,
prefetchable and repeatable by a browser doing ordinary browser things.

The invariant was replaced with a narrower, stronger pair, both enforced by
tests:

1. no route in the system can reach a venue order endpoint — still no
   `TradingPort` implementation, still no Binance order path named anywhere;
2. writes exist **only** under `/paper`, and every one acts on in-memory
   simulation state. There is no PUT, PATCH or DELETE anywhere, and CORS
   advertises exactly the two verbs that exist.

A refusal is a `200` carrying `accepted: false` and a `RISK_REJECTED_*` code,
not an error page. Refused orders stay in the account history — a blank where an
order was refused is how a risk limit becomes invisible.

### Idempotency

`client_order_id` is a caller-supplied key. Resubmitting one returns the
original outcome with `idempotent_replay: true` and opens nothing new, so a
retry after a dropped response cannot double a position. Rejections are replayed
as rejections.

## 11. Verified against live Binance data

Real prices, 21 September 2026:

```
BTCUSDT  margin 20  1x  → REFUSED  RISK_REJECTED_EXCHANGE_PRECISION
                           "After truncating to the venue's step size of 0.001,
                            the order rounds to zero"
BTCUSDT  margin 85  1x  → FILLED   0.001 @ 81,569.20 (best_ask), fee 0.0408
         close           → net -0.0979 USDT, return -0.12%
ETHUSDT  margin 10  1x  → REFUSED  RISK_REJECTED_MIN_NOTIONAL
                           "Notional 7.98846 is below the venue's published
                            minimum of 20 for ETHUSDT"
ETHUSDT  margin 25  1x  → FILLED   0.009 short @ 2,663.00 (best_bid)
ETHUSDT  leverage 10    → REFUSED  RISK_REJECTED_MAX_LEVERAGE
                           exchange_max null, risk_max null, approved null
retry same client_order_id → idempotent_replay: true, same order_id
balance == starting + realized → true at every step
```

### A real constraint worth knowing about

**BTCUSDT's step size is 0.001 BTC — about 80 USDT per increment.** With the
default 100 USDT balance at 1x, a BTCUSDT position needs roughly 82+ USDT of
margin; anything smaller truncates to zero and is refused. ETHUSDT publishes a
20 USDT minimum notional. Neither is a limitation of this engine — they are
Binance's published filters, honoured rather than approximated, which is what
makes a paper size one that could actually have been submitted.

For a 100 USDT account, prefer instruments where `step_size × price` is small,
or raise the balance with `POST /paper/reset {"starting_balance": "1000"}`.

## 12. Not modelled

Funding payments · partial fills, order-book depth and queue position · maker
rebates and fee tiers · borrow costs · exchange downtime and venue-side order
rejection · maintenance-margin liquidation tiers.

**Liquidation is optimistic.** It fires when loss equals posted margin. Real
venues liquidate *earlier*, at a maintenance-margin threshold that varies by
symbol and notional tier, and charge a fee for it. At 1x a long has **no**
liquidation price at all — `null`, not `0.00`, because a level of zero is not a
liquidation price. A 1x short still has one, at twice entry: a short's loss is
unbounded above.

Every item here is returned on `GET /paper/method` and rendered on the terminal
beside the numbers.

## 13. Not in this phase

**Autonomous paper trading** is registered `PLANNED`. `autonomous_enabled` is
`false`, there is no setter, and no loop exists to turn on — a toggle that did
nothing would be worse than no toggle.

Partial fills, a persistent store, multi-account isolation, and any form of real
execution all belong to later phases and none is claimed here. Ask the running
service: `GET /api/v1/system/capabilities`.
