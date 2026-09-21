# Phase 7 — Risk Engine and Autonomous Paper Trading

> **Design only. No code exists for this phase.** This document is for review;
> nothing in it has been implemented, and nothing should be until it has been
> agreed. Status of every capability named here remains `PLANNED` in the
> registry until its tests land.

## 0. Scope

Phase 7 delivers two things that belong together:

1. **The risk engine** (`risk.engine`, registry phase 7) — the mode-independent
   authority that rules on every proposed order, whatever proposed it.
2. **The autonomous paper-trading loop** (`paper.autonomous`, registry phase 7)
   — a server-side loop that proposes orders from strategy output without a
   human, and is refused by the risk engine exactly like a human would be.

They ship together because an autonomous loop without a risk engine is a
strategy with a keyboard, and a risk engine with nothing autonomous to refuse
is untested authority.

**Out of scope, explicitly:** testnet execution, live execution, API
credentials, withdrawal, order placement at any venue, machine learning,
hyperparameter optimisation, and multi-account isolation.

---

## 1. The finding that shapes this phase

**Building the risk engine does not unlock leverage above 1x.**

The chain is `approved = min(requested, exchange_max, risk_max)`, and it fails
closed on any unknown. Phase 7 makes `risk_max_leverage` knowable — the risk
engine can derive a ceiling from volatility, stop distance, equity and the
daily loss limit. But `exchange_max_leverage` remains **unknown**, because
Binance serves per-symbol leverage brackets only from an authenticated endpoint
(`/fapi/v1/leverageBracket`) and there is no public equivalent. This build holds
no credentials and phase 7 does not introduce any.

So after phase 7, a request for 3x still resolves to:

```
requested 3 · exchange_max null · risk_max 2 · approved null
reason: RISK_REJECTED_EXCHANGE_MAX_LEVERAGE_UNKNOWN
```

Autonomous paper positions will open at **1x**, via the domain-minimum rule
([ADR 0004](adr/0004-leverage-at-the-domain-minimum.md)), exactly as manual ones
do today. That is the fail-closed architecture working as specified, not a
defect — but it means "the risk engine now enforces leverage" would be a false
claim, and the phase must not make it. What phase 7 adds is a *risk ceiling that
is computed and reported*, binding the moment a venue ceiling becomes known.

This is stated first because it is the single most likely thing to be
misunderstood about the phase.

---

## 2. Autonomous paper-trading loop

### 2.1 Where it runs

A single `asyncio` task owned by the FastAPI application, started and stopped by
the existing `lifespan` handler.

Rejected alternatives:

- **A separate worker process.** Correct at scale, but it needs shared state,
  which needs the database from phase 1. With in-memory paper state a second
  process would trade a different account.
- **Frontend-driven, as phase 6's `POST /paper/tick` is.** Unacceptable for
  autonomy: stops would be evaluated only while a browser tab is open. That is
  a documented limitation of phase 6 and the thing phase 7 exists to remove.

**Budget:** the target machine has ~1 GB free RAM. One task, one bounded
universe, one candle request per symbol per iteration.

### 2.2 Cadence

| Setting | Default | Meaning |
|---|---|---|
| `AETHERIS_AUTO_INTERVAL_SECONDS` | `30` | Between iterations |
| `AETHERIS_AUTO_MAX_SYMBOLS` | `5` | Symbols evaluated per iteration |
| `AETHERIS_AUTO_TIMEFRAME` | `15m` | Decision timeframe |

An iteration that overruns the interval does **not** stack: the next tick is
skipped and the skip is recorded. Overlapping iterations against one account is
a correctness problem, not a throughput one.

### 2.3 What one iteration does

```
for each symbol in the watched universe:
    1. manage open position   (mark, SL/TP/trailing/liquidation, signal flip)
    2. evaluate strategy      (closed bars only)
    3. propose entry          (if flat, and a bias exists)
    4. RISK ENGINE            (refuse, or approve with an approved size)
    5. submit to the paper engine   (which runs its own gate again)
record one AutonomousDecision per symbol, always, including "no action"
```

**Management before entry, deliberately.** A position that should have closed
must not still be occupying a slot when the entry check counts open positions.

---

## 3. Risk engine: final authority

### 3.1 Shape

New package `engines/risk/`, mode-independent:

```
engines/risk/
  policy.py     the envelope: limits, ceilings, cooldowns  (pure)
  engine.py     evaluate(proposal, account_state, market) -> RiskVerdict
  leverage.py   derives risk_max_leverage from measured inputs (pure)
```

`RiskVerdict` is either an approval carrying an **approved size and approved
leverage**, or a refusal carrying a `RiskRejectionCode` and a sentence. There is
no third outcome and no partial approval that a caller may reinterpret.

### 3.2 Relationship to the phase 6 paper gate

`engines/paper/risk.py` stays. Phase 6's `check_authority` / `check_market_data`
/ `check_entry` are the paper engine's own gate and are already tested; the risk
engine does not replace them.

**Layering:** the risk engine sits *above* the paper engine and rules first; the
paper gate then re-checks independently. Two gates is not redundancy to be
optimised away — it is the same principle that makes `resolve_leverage`
re-validate its own input. The autonomous loop cannot reach the paper engine
without passing the risk engine, and could not bypass the paper gate even if it
did.

This means **phase 7 touches phase 6 files**. Expected changes are additive:
`engines/paper/risk.py` gains nothing; `engines/paper/engine.py` gains an
optional `risk_verdict` argument recorded on the order for audit. If you would
rather phase 6 stay byte-frozen, the alternative is to record the verdict in the
autonomous decision log only — cheaper, but the order loses its provenance.
**Flagged for your decision (§13.2).**

### 3.3 What the risk engine evaluates

Inputs, all measured or configured — none inferred:

| Input | Source |
|---|---|
| Account equity, balance, margin used | paper account snapshot |
| Open positions and aggregate exposure | paper account snapshot |
| Day's realised PnL and lock state | `DailySession` |
| ATR / realised volatility | phase 4 indicators over closed bars |
| Proposed stop distance | the proposal |
| Symbol filters (step, tick, min notional) | venue metadata |
| Venue leverage ceiling | **null** — see §1 |
| Time since last entry on this symbol | decision log |

Refusal codes are drawn **entirely from the existing `RiskRejectionCode`
vocabulary**. Phase 7 introduces no new codes. Two that exist and have never
fired get wired up:

- `COOLDOWN` — `entry_cooldown_seconds` (default 300) is already in
  `RiskSettings` and currently unused.
- `ABNORMAL_VOLATILITY` — refuses entry when measured ATR% exceeds a configured
  ceiling. Defined as a *measurement* threshold, never a forecast.

`STRATEGY_LIMIT` and `RECONCILIATION_PENDING` remain unused and stay that way
until something needs them.

---

## 4. `autonomous_enabled` defaults to false

Three independent conditions, all required:

1. `AETHERIS_AUTONOMOUS_TRADING_ENABLED=true` — already exists, defaults false.
   Gates *availability*: with it false, the toggle endpoint refuses and the loop
   task is never created.
2. A deliberate `POST /api/v1/paper/autonomous {"enabled": true}` at runtime.
3. Paper mode enabled (`is_mode_enabled(PAPER)`).

Config alone never starts the loop. This mirrors the two-switch pattern live
trading already uses, and follows the standing principle that **modes never
escalate themselves**.

**The runtime arm does not survive a restart** (§12).

---

## 5. Daily profit target: +20 USDT
## 6. Daily loss limit: −10 USDT

Unchanged from phase 6, and deliberately *not* reimplemented. The autonomous
loop proposes; `DailySession` and the existing lock logic refuse. Same code
path, same `RISK_REJECTED_DAILY_PROFIT_TARGET` / `_DAILY_LOSS_LIMIT` codes, same
evaluation on **realised** PnL, same UTC-day reset.

Two behaviours phase 7 must preserve and test under autonomy:

- A lock stops **opening only**. The loop keeps managing open positions —
  stops, targets and trailing continue to fire while locked. An autonomous loop
  that stopped managing when locked would leave positions unattended at exactly
  the wrong moment.
- No position is ever force-closed by a lock.

When locked, the loop continues iterating (to manage) but skips step 3, and
records the skip with its code. It does not sleep until midnight — the lock can
clear only at the day boundary, but positions need attention before then.

---

## 7. Position sizing and leverage enforcement

### 7.1 Sizing

The loop derives a size; it never asks a human for one.

```
margin = available_balance × AETHERIS_AUTO_POSITION_SIZE_PERCENT / 100
```

Default **10%**, matching the backtest engine's `position_size_percent`
semantic so the two remain comparable. Measured against *available* balance
(balance − margin used), not equity: unrealised profit does not fund new
positions, consistent with phase 6.

The resulting margin then passes through the unchanged phase 6 path — venue
step size, minimum notional, per-position and portfolio ceilings — and is
refused there if it does not fit.

**Consequence worth stating before it surprises anyone:** on a 100 USDT account
at 10%, margin is ~10 USDT. BTCUSDT's step size is 0.001 BTC (~80 USDT per
increment) and ETHUSDT publishes a 20 USDT minimum notional, so the loop will
refuse both with `EXCHANGE_PRECISION` / `MIN_NOTIONAL` on most iterations. This
is venue reality honoured correctly, and the audit trail will show it plainly —
but a loop that refuses everything is not a useful demonstration. Mitigations,
in preference order:

1. Select the universe with affordability as a hard filter (§13.1) — only watch
   symbols whose minimum tradable notional fits the budget.
2. Raise the paper balance (`POST /paper/reset {"starting_balance": "1000"}`).
3. Raise `AUTO_POSITION_SIZE_PERCENT`.

None of these changes a venue constraint; they choose instruments and sizes that
fit it.

### 7.2 Leverage

The loop requests `AETHERIS_AUTO_REQUESTED_LEVERAGE` (default **1**) and the
existing chain rules on it. Per §1, anything above 1 resolves to `null` and the
order is refused with `MAX_LEVERAGE`. The default is 1 so the loop is functional
rather than perpetually self-refusing; raising it is a way to *observe the
refusal*, not to obtain leverage.

The risk engine additionally computes and reports `risk_max_leverage`. It binds
as soon as a venue ceiling exists. Until then it is reported and non-binding,
and the design must not describe it as "enforcing" anything.

**Never:** strategy condition counts, bias strength or any score may influence
`requested_leverage` beyond the existing deterministic ATR-based candidate, and
none of them may ever authorise leverage.

---

## 8. Entry/exit decision flow

### 8.1 Entry

```
StrategyResult.status is not READY   → no action, record status as the reason
StrategyResult.bias is NEUTRAL       → no action, record NEUTRAL
position already open in this symbol → no entry (management only)
bias is LONG_BIAS / SHORT_BIAS       → propose entry in that direction
```

The bias is a **deterministic reading of named conditions**, not a signal
strength, probability or confidence. The decision record carries the condition
counts (`3/4`) as counts, never as a ratio presented as a likelihood.

### 8.2 Exit

Four paths, three already built:

| Path | Status |
|---|---|
| Stop loss, take profit, trailing stop, liquidation | phase 6, unchanged |
| `SIGNAL_FLIP` — bias no longer supports the open direction | **new in phase 7** |
| Manual close via the API | phase 6, unchanged |
| Daily lock | never closes anything, by design |

`PaperExitReason.SIGNAL_FLIP` already exists in the phase 6 domain and is
currently unused. Phase 7 wires it: an open LONG whose strategy now reads
`SHORT_BIAS` is closed. A read of `NEUTRAL` does **not** close — neutral means
the conditions no longer align, not that they oppose, and closing on neutral
would churn the account on every indecisive bar.

Flip-closing applies only under autonomy and only when
`AETHERIS_AUTO_CLOSE_ON_FLIP` (default **true**) is set.

### 8.3 Stops and targets

Attached at entry from configuration, since no human supplies them:

| Setting | Default |
|---|---|
| `AETHERIS_AUTO_STOP_LOSS_PERCENT` | `2` |
| `AETHERIS_AUTO_TAKE_PROFIT_PERCENT` | `4` |
| `AETHERIS_AUTO_TRAILING_STOP_PERCENT` | unset (off) |

Matching the backtest defaults, again for comparability.

---

## 9. SL/TP/trailing management

Phase 6's `PaperEngine.tick()` already implements all of it — level evaluation,
nearest-adverse-level-wins, adverse-beats-target, trailing ratchet, optimistic
liquidation. **Phase 7 adds no new management logic.** It changes only *who
calls it*: a server-side loop instead of a browser.

Two consequences:

- **Management no longer requires the page open.** This is a behavioural change
  to phase 6's documented limitation and must be re-documented in
  `paper-trading.md` §7 and on the terminal, which currently states the
  opposite. The statement becomes conditional: management is continuous *while
  the autonomous loop is running*, and poll-driven otherwise.
- **The poll-fidelity caveat stands.** A level is still noticed on an iteration
  rather than the instant it is touched, so a fill still uses the price observed
  then and a realised loss can still exceed the stop distance. A 30s loop
  interval makes this *smaller*, not gone, and the documentation must not imply
  otherwise.

`POST /paper/tick` remains and stays safe to call with the loop running (§14.4).

---

## 10. Stale and unusable market data

No change in policy, and none is wanted: phase 6 already refuses outright on
`UNAVAILABLE`, `STALE`, unverifiable age, or age beyond
`max_data_age_seconds` (30s), and never substitutes a price.

What phase 7 adds is **loop behaviour on bad data**:

- A symbol whose data is unusable is **skipped for the iteration**, with the
  status recorded. It is not retried within the iteration and does not abort the
  others.
- An unmarkable open position is left alone and reports `null` PnL, exactly as
  today. It is **not** closed — closing at an invented price is the failure this
  rule exists to prevent.
- Consecutive failures are counted per symbol. Beyond
  `AETHERIS_AUTO_MAX_CONSECUTIVE_FAILURES` (default 10) the symbol is dropped
  from the universe for the session and the drop is recorded. This bounds a
  delisted or permanently broken symbol's cost.
- If **every** symbol fails for `AETHERIS_AUTO_OUTAGE_ITERATIONS` consecutive
  iterations (default 5), the loop records a venue-outage condition and backs
  off to a longer interval until one succeeds. It does not stop: an outage is
  not a reason to abandon open positions.

---

## 11. Idempotency and duplicate-order protection

Phase 6's `client_order_id` mechanism is the whole mechanism; phase 7 supplies a
**deterministic key** rather than adding a second one.

```
client_order_id = "auto-{symbol}-{timeframe}-{bar_close_epoch_ms}-{side}"
```

This makes duplicate protection structural rather than a flag:

- A retry after a transient failure recomputes the same id and replays the
  original outcome (`idempotent_replay: true`), opening nothing new.
- **At most one autonomous entry per symbol per closed bar** falls out of the
  key for free. Re-evaluating the same bar — because the loop ran twice within
  one 15m candle — cannot open a second position.
- Because rejections are stored under their key too, a refused bar is not
  retried on the next iteration. That is intended: the refusal reason
  (insufficient balance, cooldown, lock) is unlikely to have changed mid-bar,
  and retrying it every 30s would flood the decision log.

**Bar close time comes from the candle series, not the clock**, so it is the
same value on every iteration within a bar.

**Known interaction:** the phase 6 order log evicts by age past
`max_order_log` (200), which also evicts the idempotency index entry. At one
entry per symbol per bar and five symbols, 200 orders is many hours of history —
but the loop's refusals also consume the log. Phase 7 must either raise the
default for autonomous operation or key the idempotency index separately.
**Recommended:** raise `max_order_log` default to 1000 when autonomy is armed,
and add a test that a replay still works after heavy refusal traffic.

---

## 12. Emergency stop

Existing behaviour is correct and unchanged: `EMERGENCY_STOP` blocks entries,
never closes positions, and is checked first in the refusal ordering.

Phase 7 additions:

- Engaging the emergency stop **also disarms autonomy**. An operator hitting
  the stop expects the loop to stop proposing, not to keep proposing and keep
  being refused. Both facts are recorded.
- Releasing the emergency stop does **not** re-arm autonomy. Re-arming is a
  separate deliberate act. Auto-resuming on release is the kind of convenience
  that turns a safety control into a surprise.
- The loop keeps running while stopped, purely to manage open positions. It
  records each skipped entry with `EMERGENCY_STOP`.

---

## 13. Restart and recovery semantics

Paper state is in-memory, so a restart is a **total loss of state**, not a
recovery problem. The design's job is to make that safe and obvious rather than
to pretend otherwise.

On startup:

1. A fresh account at the configured starting balance (phase 6 behaviour).
2. **`autonomous_enabled` is `false`**, regardless of what it was before the
   restart. This is the most important safety property in the section: a process
   that crashed and came back trading, unattended, against an account it does
   not remember, is the worst available outcome. Re-arming requires a human.
3. The decision log starts empty.
4. `GET /paper/reconciliation` continues to report `NOT_APPLICABLE` — there is
   no external authority to disagree with, and claiming `CONSISTENT` would imply
   a check that never happened.

**When durable state arrives (phase 1+),** the rules become: positions are
reloaded, `RECONCILIATION_PENDING` blocks all entry until a reconciliation pass
completes, and autonomy still requires a fresh human arm. The
`ReconciliationReport` hook and the `RECONCILIATION_PENDING` code already exist
for exactly this; phase 7 does not use them and does not pretend to.

**The loop must also survive its own death.** If the task raises, the supervisor
records the exception, disarms autonomy, and does not silently restart. A loop
that restarts itself after an unexplained crash hides the crash.

---

## 14. Audit and reason-code trail

### 14.1 Every iteration produces a record, including "nothing happened"

New frozen domain model `AutonomousDecision` in `domain/autonomous.py`:

| Field | Purpose |
|---|---|
| `decision_id`, `sequence`, `decided_at` | ordering and reference |
| `symbol`, `timeframe`, `bar_close_time` | what was evaluated |
| `action` | `ENTERED` · `REFUSED` · `MANAGED` · `CLOSED` · `NO_SIGNAL` · `SKIPPED` |
| `strategy_status`, `bias`, `conditions_met`, `conditions_total` | the reading, as counts |
| `leverage` | the full `LeverageDecision` chain |
| `rejection_code`, `rejection_detail` | the `RISK_REJECTED_*` code and its sentence |
| `risk_verdict` | the risk engine's ceilings and what bound |
| `client_order_id`, `order_id`, `position_id`, `trade_id` | links into paper state |
| `data_status`, `data_age_seconds`, `source` | provenance of what it decided on |

A `NO_SIGNAL` record is as important as an `ENTERED` one. "The loop did nothing
for six hours" must be distinguishable from "the loop was not running", and both
from "the loop errored silently".

### 14.2 Bounds

In-memory, capped at `AETHERIS_AUTO_MAX_DECISION_LOG` (default 500), oldest
evicted. Same reasoning as the order log: this list otherwise grows for as long
as the process runs, on a machine with ~1 GB free.

### 14.3 Structured logs

One `structlog` line per decision, at `info` for actions and `debug` for
`NO_SIGNAL`. Reusing the phase 0 logger and its request-context binding.
**No credential, no secret, and no account identifier in a log line** — there is
nothing of the sort to leak, and the logging test asserts it stays that way.

### 14.4 New read endpoints

| Route | Method | Returns |
|---|---|---|
| `/api/v1/paper/autonomous` | GET | armed state, interval, universe, last iteration, counters |
| `/api/v1/paper/autonomous/decisions` | GET | the bounded decision log |
| `/api/v1/paper/autonomous` | POST | arm / disarm |

One new write route, under the existing `/paper` namespace, so
[ADR 0003](adr/0003-write-routes-and-the-get-only-invariant.md)'s invariant
holds unchanged: writes exist only under `/paper`, against simulation state.
**No amendment to ADR 0003 is required.**

---

## 15. No fabricated prices, signals, confidence, win rates or predictions

Enforced the same way phase 6 enforces it — structurally, not by review.

| Never | Instead |
|---|---|
| A price when data is unusable | refusal with `STALE_DATA`; position marks `null` |
| A confidence or probability on a decision | condition counts (`3/4`), as counts |
| A win rate or expected return from the loop | realised trade history, labelled as history |
| An ML prediction | none exists; `ai.analysis` stays `PLANNED` (phase 9) |
| A "signal strength" scalar | the named conditions and whether each held |
| A venue leverage ceiling | `null`, always, until an authenticated source exists |

**Vocabulary that must not appear** in `AutonomousDecision`, any endpoint, any
log line or any UI string: *confidence*, *probability of profit*, *win
probability*, *expected return*, *prediction*, *forecast*, *AI-selected*,
*guaranteed*. A source-level test asserts their absence in the phase 7 modules,
extending the pattern already used for credential tokens and venue names.

The Market Opportunity Score, if used for universe selection (§18.1), is a
**ranking metric for where to look** — never an input to whether to trade, and
never described as predictive. This distinction needs to survive review.

---

## 16. No live or testnet order path

Unchanged and re-asserted:

- Nothing implements `TradingPort`. The autonomous engine will be covered by the
  same architecture test that already asserts this of `PaperEngine`.
- No venue order path (`/fapi/v1/order`, `batchOrders`, `/fapi/v2/account`),
  no testnet host, no `set_leverage`, no `marginType` — asserted against source
  across the whole package.
- `engines/risk/` and the autonomous loop join the purity test: no `httpx`, no
  `fastapi`, no `starlette`, no venue name, no adapter import. Only
  `services/` touches the network.
- No credential concept — no `api_key`, `api_secret`, `signature`, `hmac`,
  `withdraw` — may appear even as a field name. Already tested for
  `engines/paper/`; extended to `engines/risk/` and the autonomous module.
- `execution.testnet` and `execution.live` remain `PLANNED`.

---

## 17. Separation between paper autonomy and future testnet/live execution

This is the boundary most at risk of eroding, because "the loop works, point it
at testnet" is a small-looking change. Three structural barriers:

1. **The loop depends on `PaperEngine`, concretely, not on an execution
   interface.** There is deliberately no `ExecutionPort` that a testnet adapter
   could later satisfy. Pointing the loop at a venue requires writing new code
   and changing its type, not changing a config value. When testnet arrives, it
   gets its own orchestrator with its own gates.
2. **The mode is not a parameter.** Nothing in the loop takes a `TradingMode`;
   it is a paper component and reads `PAPER` where a mode is needed. There is no
   `mode=LIVE` to pass.
3. **The arming endpoint lives under `/paper/`**, not a neutral `/autonomous/`.
   A future testnet loop needs its own route, its own switch and its own
   acknowledgement.

Documented in `paper-trading.md` §2 alongside the existing paper/testnet/live
table, which already states that testnet is **not** a safer paper mode — it
places real orders with a real credential.

---

## 18. Interaction with the phase 6 boundaries

### 18.1 Universe selection

The loop needs to know which symbols to watch. **Unresolved — see §20.1.**
Recommended default: a configured explicit list
(`AETHERIS_AUTO_SYMBOLS`, default empty = loop does nothing and says so),
because it is the only option with no hidden selection policy.

### 18.2 Layering

```
AutonomousLoop            (services/ — owns the task, the clock, the network)
   ├── AnalysisService    (phase 4, unchanged)
   ├── RiskEngine         (phase 7, pure)
   └── PaperTradingService (phase 6, unchanged)
         └── PaperEngine   (phase 6)
               └── paper risk gate (phase 6)
                     └── PaperRepository (phase 6)
```

The loop **never touches `PaperRepository` directly.** Every mutation goes
through `PaperTradingService`, which is the boundary that already fetches marks,
resolves leverage and calls the engine. This is what keeps the durable
repository a drop-in replacement.

### 18.3 Concurrency — the one genuinely new hazard

Phase 6 had a single writer: the HTTP request. Phase 7 adds a second: the loop.
Both do read-modify-write across an `await` (fetching market data between
loading state and mutating it), so two in-flight operations can interleave at
the await point and the second can act on a stale view.

`PaperEngine`'s methods are synchronous and therefore atomic within the event
loop, so state cannot be *corrupted* — but two concurrent submissions could both
pass the open-position check before either opened.

**Required:** an `asyncio.Lock` in `PaperTradingService` around each
fetch-then-mutate sequence (`submit_order`, `tick`, `close_position`, `reset`,
`set_emergency_stop`). Reads (`get_account`) do not take it.

This is an additive change to a phase 6 file and must be tested with a
deliberately interleaved pair of concurrent submissions.

### 18.4 Session and day rollover

`_ensure_session` already handles the UTC boundary. The loop must not cache a
session across iterations — it reads it fresh each time, so a rollover mid-run
is picked up. An iteration that *spans* midnight attributes each decision to the
day in force when that decision was made; no back-dating.

---

## 19. Assumptions

1. **The venue leverage ceiling stays unknown** through phase 7. No public
   endpoint publishes per-symbol brackets, and no credential is introduced.
   *If wrong:* leverage above 1x becomes approvable and §7.2 needs revisiting.
2. **Paper state stays in-memory.** Phase 1 (auth + PostgreSQL) remains blocked
   on the database decision in [ADR 0002](adr/0002-database-hosting.md).
   *If wrong:* §13 gains real recovery semantics and `RECONCILIATION_PENDING`
   gets wired.
3. **One account, one process.** No multi-tenancy, no horizontal scaling.
4. **`trend_momentum` remains the only registered strategy.** The loop is
   written against the registry, not against that strategy specifically, but
   only one will be exercised.
5. **Closed bars only.** The loop decides on closed candles; the forming bar is
   dropped, as phase 4 already does. No intra-bar entries.
6. **REST polling, no websockets.** Consistent with the RAM budget and every
   prior phase.
7. **The machine stays awake while the loop runs.** A laptop that sleeps stops
   managing positions; the design does not attempt to compensate, and the
   documentation should say so.
8. **The default 100 USDT balance makes most symbols untradable** at 10% sizing
   (§7.1). Assumed acceptable for a demonstration; mitigations listed.

---

## 20. Unresolved decisions

Each has a recommendation. None blocks writing the code once chosen.

### 20.1 Universe selection — the one I most want a decision on

| Option | For | Against |
|---|---|---|
| **A. Explicit configured list** *(recommended)* | No hidden policy. Reproducible. Trivial to reason about. | Static; needs manual curation. |
| B. Top-N by scanner opportunity score | Reuses a built, bounded, published-formula component. | Risks the score being read as a trade signal — the spec forbids describing it that way, and using it to *pick trades* edges toward exactly that. |
| C. Top-N by 24h quote volume | Purely liquidity-based, no analytical claim. | Ignores affordability; would pick BTCUSDT and refuse it. |
| D. Affordability-filtered volume | Only watches what the balance can actually trade. | Slightly more machinery; selection rule needs documenting. |

**Recommendation: A, with D as a filter applied on top.** An explicit list, then
drop any symbol whose minimum tradable notional exceeds the sizing budget, and
record the drop. Default empty — the loop reports "no universe configured"
rather than choosing for the user.

### 20.2 Does the paper order record the risk verdict?

Recommended **yes** (§3.2), accepting a small additive change to
`engines/paper/engine.py`. Alternative: keep phase 6 byte-frozen and record the
verdict only in the decision log, losing the order's provenance link.

### 20.3 Order log ceiling under autonomy

Recommended: raise `max_order_log` default to 1000 when armed (§11).
Alternative: a separate, longer-lived idempotency index.

### 20.4 Does `SIGNAL_FLIP` close on `NEUTRAL`?

Recommended **no** (§8.2) — neutral is indecision, not opposition, and closing
on it churns. Configurable if you disagree.

### 20.5 Loop interval

Recommended 30s against a 15m decision timeframe. The interval governs
management latency, not decision frequency (one entry per bar regardless).
Shorter improves stop fidelity and costs venue quota.

### 20.6 Should the loop back off when the daily lock engages?

Recommended **no** (§6) — it must keep managing. Alternative is a longer
interval while locked, which trades stop fidelity for quota.

---

## 21. Failure modes

| # | Failure | Detection | Response |
|---|---|---|---|
| 1 | Venue outage mid-iteration | `AetherisError` from the adapter | Skip symbol, record, continue; back off after N total-failure iterations (§10) |
| 2 | Loop task raises | Supervisor wrapper | Record, **disarm autonomy**, do not auto-restart (§13) |
| 3 | Iteration overruns the interval | Timestamp comparison | Skip the next tick, record; never stack |
| 4 | Loop and HTTP request mutate concurrently | — | `asyncio.Lock` in the service (§18.3); regression test with interleaved submits |
| 5 | Duplicate entry on the same bar | — | Deterministic `client_order_id` (§11) |
| 6 | Idempotency key evicted by log churn | — | Raised log ceiling (§20.3) + test |
| 7 | Decision log grows unbounded | — | Capped at 500, oldest evicted (§14.2) |
| 8 | Day rolls over mid-iteration | Fresh session read per decision | Attribute to the day in force; no back-dating |
| 9 | Strategy raises on malformed candles | try/except per symbol | Record `SKIPPED` with the exception type; do not abort the iteration |
| 10 | Position unmarkable for many iterations | Consecutive-failure counter | Leave open, report `null` PnL, **never close at an invented price** |
| 11 | Symbol delisted mid-session | Repeated `SymbolNotFoundError` | Drop from universe after N, record |
| 12 | Machine sleeps / process suspended | Gap between iteration timestamps | Record the gap; management resumes with current prices, not backfilled ones |
| 13 | Emergency stop engaged mid-iteration | Checked per proposal | Remaining entries refused with `EMERGENCY_STOP`; management continues |
| 14 | Restart with autonomy previously armed | — | Starts disarmed, always (§13) |
| 15 | Clock moves backwards (NTP correction) | `bar_close_time` from candles, not the clock | Idempotency unaffected; a decision timestamp may repeat and is recorded as-is |
| 16 | Rate limit from the venue | `EXCHANGE_RATE_LIMITED` | Existing adapter backoff; loop treats as a skip and lengthens its interval |

---

## 22. Security boundaries

1. **No credentials exist.** Not in config, not in the loop, not as a field
   name. Asserted at source level across `engines/` and the new modules.
2. **No new outbound surface.** The loop uses the same read-only
   `MarketDataPort` as every other component. No new host, no new endpoint.
3. **No new inbound surface beyond `/paper`.** One write route (arm/disarm), two
   reads. ADR 0003's invariant is unchanged.
4. **Arming is a deliberate POST**, gated behind a config flag that defaults
   false, and does not survive restart. Three conditions, all human (§4).
5. **No account identifier is accepted from a request.** Phase 6's
   single-server-side-account property is preserved, which is what keeps this
   free of an IDOR while authentication does not exist.
6. **No dynamic execution.** Strategy and indicator selection remain closed
   whitelists of registered callables; the existing test banning `eval`, `exec`,
   `compile`, `__import__`, `os.system` and `subprocess` covers the new modules.
7. **Configuration is operator-controlled, never request input.** The universe,
   interval and sizing come from environment settings; no API parameter can
   redirect what the loop watches or how large it sizes.
8. **Logs carry no secrets and no PII.** Nothing of the sort exists to leak.
9. **Resource bounds are security-relevant** on a 1 GB machine: capped universe,
   capped decision log, capped order log, one non-stacking task.

---

## 23. Tests required

**Risk engine (unit, pure)**
1. Each `RiskRejectionCode` the engine can emit, emitted for its own cause
2. Refusal ordering: most fundamental reason wins (extends the phase 6 pattern)
3. `risk_max_leverage` derived from measured inputs, never from bias or counts
4. A verdict never approves a size above any configured ceiling
5. `ABNORMAL_VOLATILITY` fires on measured ATR%, and is never described as a forecast
6. `COOLDOWN` fires within `entry_cooldown_seconds` and not after
7. Approved size always satisfies venue step / min-notional

**Autonomous loop (unit, with a fake clock and injected marks)**
8. Disarmed by default; config alone does not arm it
9. Arming requires config **and** the runtime POST
10. One entry per symbol per closed bar, however many iterations run
11. Deterministic `client_order_id`; a replay opens nothing new
12. `NO_SIGNAL` recorded when bias is `NEUTRAL`
13. No action and a recorded status when `StrategyStatus` is not `READY`
14. Unusable data skips the symbol without aborting the iteration
15. An unmarkable open position is left open with `null` PnL, never closed
16. Daily lock blocks entries but management still fires stops
17. Emergency stop disarms autonomy; release does not re-arm
18. `SIGNAL_FLIP` closes an opposed position; `NEUTRAL` does not
19. Overrunning iteration skips rather than stacks
20. Loop exception disarms and does not auto-restart
21. Symbol dropped after N consecutive failures
22. Decision log bounded; oldest evicted
23. Every iteration produces a record, including a fully idle one
24. Day rollover mid-run attributes correctly

**Integration (HTTP, mocked venue)**
25. Arm/disarm round trip; state reported honestly
26. Decision log endpoint shape and bounds
27. `assert_route_surface` still passes — writes only under `/paper`
28. Capability registry: `risk.engine` and `paper.autonomous` `AVAILABLE` only
    in the commit that lands their tests
29. Restart semantics: a fresh app starts disarmed

**Architecture / source-level**
30. `engines/risk/` and the autonomous module are pure (no httpx, fastapi,
    adapter, venue name)
31. Nothing implements `TradingPort`; no venue order path anywhere
32. No credential or withdrawal token in the new modules
33. **No forbidden vocabulary** (§15) in the new modules or their API strings
34. No dynamic execution in the new modules

**Concurrency**
35. Interleaved loop + HTTP submission cannot open two positions in one symbol
36. Interleaved tick + close cannot double-realise a trade

**Accounting invariant**
37. `balance == starting_balance + realized_pnl` holds across a full autonomous
    session, not just a manual round trip

**Frontend**
38. Autonomy status and the arm/disarm control render; disarmed is visibly the
    default
39. The decision log renders, including `NO_SIGNAL` and refusal rows
40. Card fallbacks for every new table (the phase 6 regression)
41. The page states that autonomy is a simulation and places no real order

**Live + browser verification** (as every phase since 3)
42. Against real Binance data, with the loop armed, for at least one full
    decision bar
43. Browser at five viewport widths

---

## 24. Files and modules that would change

**New**

```
backend/src/aetheris/domain/autonomous.py         AutonomousDecision, action enum, status
backend/src/aetheris/engines/risk/__init__.py
backend/src/aetheris/engines/risk/policy.py       the envelope (pure)
backend/src/aetheris/engines/risk/engine.py       evaluate() -> RiskVerdict (pure)
backend/src/aetheris/engines/risk/leverage.py     risk_max_leverage derivation (pure)
backend/src/aetheris/services/autonomous.py       the loop, the task, the supervisor
backend/src/aetheris/api/v1/autonomous.py         or added to api/v1/paper.py
backend/tests/unit/test_risk_engine.py
backend/tests/unit/test_autonomous_loop.py
backend/tests/integration/test_autonomous_api.py
frontend/src/app/paper/autonomous/…               or a panel on the paper page
docs/adr/0005-autonomous-loop-placement.md        in-process task vs worker
```

**Modified**

| File | Change | Risk |
|---|---|---|
| `core/config.py` | `AutonomousSettings` block | low, additive |
| `core/capabilities.py` | `risk.engine`, `paper.autonomous` → `AVAILABLE`; phase 7 → `DELIVERED_PHASES` | low |
| `main.py` | build the loop, wire lifespan start/stop | **medium** — startup path |
| `services/paper.py` | `asyncio.Lock` around fetch-then-mutate (§18.3) | **medium** — touches phase 6 |
| `engines/paper/engine.py` | optional `risk_verdict` on the order, if §20.2 is yes | low, additive, **phase 6** |
| `api/v1/paper.py` | three autonomous routes | low |
| `api/deps.py` | `AutonomousDep` | low |
| `tests/unit/test_architecture.py` | purity + vocabulary for new modules | low |
| `tests/unit/test_capabilities.py` | `paper.autonomous` leaves the not-claimed list | low |
| `frontend/src/lib/types.ts`, `api.ts` | autonomous types and client calls | low |
| `frontend/src/app/paper/page.tsx` | status, arm/disarm, decision log | medium |
| `docs/paper-trading.md` | §7 no longer "only while the page is open" (§9) | **must not be missed** |
| `docs/terminal.md`, `docs/architecture.md`, `docs/setup.md`, `README.md` | phase 7 status, new settings | low |

**Highest-risk edits:** `main.py` (a broken lifespan breaks every endpoint) and
`services/paper.py` (a lock in the wrong place deadlocks the loop against an
HTTP request). Both need their own tests before anything else is written.

---

## 25. What I need from the review

1. **§20.1 — universe selection.** My recommendation is an explicit configured
   list with an affordability filter, defaulting to empty. This is the decision
   with the most product judgement in it.
2. **§20.2 — may phase 7 make an additive change to `engines/paper/engine.py`**
   to record the risk verdict on the order, or should phase 6 stay frozen?
3. **Confirmation of §1** — that a risk engine which still cannot approve
   leverage above 1x is the expected and acceptable outcome of this phase.

Everything else in §20 has a recommendation I am content to proceed on unless
you say otherwise.
