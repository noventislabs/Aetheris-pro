# Risk engine and autonomous paper trading

> Status: Phase 7. **Simulation only, and off by default.** The loop places no
> order at any exchange, holds no credential, and starts disarmed on every
> process start however it was left. The risk engine has final authority over
> every proposal and nothing upstream can override it.

## 1. What phase 7 does not do

**Building the risk engine did not unlock leverage above 1x**, and this
document says so first because it is the most likely thing to be misread.

The chain is `approved = min(requested, exchange_max, risk_max)` and it fails
closed on any unknown. Phase 7 makes `risk_max_leverage` knowable — the engine
derives it from stop distance, posted margin and the day's remaining loss
budget. But `exchange_max_leverage` is still **unknown**: venues serve
per-symbol leverage brackets only from an authenticated endpoint, which the
paper path does not use. (Phase 8b reads real brackets on the *testnet* path;
paper is unchanged and still refuses anything above 1x.)

Verified against live Binance data on 21 September 2026:

```
SOLUSDT 3x → RISK_REJECTED_MAX_LEVERAGE
            requested=3  exchange_max=null  risk_max=null  approved=null
            reason: RISK_REJECTED_EXCHANGE_MAX_LEVERAGE_UNKNOWN
ETHUSDT 1x → FILLED 0.018 @ 2,724.49 (best_ask), 1x
            reason: LEVERAGE_APPROVED_AT_DOMAIN_MINIMUM
```

The risk ceiling *is* computed and reported. It binds the moment a venue
ceiling exists, and not before. Describing phase 7 as "enforcing leverage"
would be false.

## 2. The risk engine

`engines/risk/`, mode-independent and pure — no I/O, no framework, no venue, no
clock of its own.

| Module | Responsibility |
|---|---|
| `policy.py` | the envelope, and the views the engine rules on |
| `leverage.py` | derives `risk_max_leverage` from measured inputs |
| `engine.py` | `evaluate(proposal, account, market, policy, now) → RiskVerdict` |

A verdict is an approval carrying an approved size and leverage, or a refusal
carrying a `RiskRejectionCode`. There is no third outcome, no partial approval
a caller could reinterpret, and the constructor rejects a verdict that claims
both.

### Two gates, deliberately

The risk engine rules first; the phase 6 paper gate then re-checks
independently before anything fills. That is not redundancy to be optimised
away — it is the same principle that makes `resolve_leverage` re-validate input
it was already handed. **A gate that assumes its caller checked is not a gate.**

The paper engine records the risk verdict on the order as provenance
(`risk_verdict_detail`) and **never reads it as permission**. Every check runs
identically whether or not something upstream already approved.

### Refusal ordering

Checks run in order of authority and the *first* thing wrong is reported:

1. `MODE_NOT_ENABLED`
2. `EMERGENCY_STOP`
3. `DAILY_PROFIT_TARGET` / `DAILY_LOSS_LIMIT`
4. `STALE_DATA` — unusable, unverifiable, or too old
5. `COOLDOWN`
6. `ABNORMAL_VOLATILITY`
7. `SYMBOL_LIMIT`, `MAX_OPEN_POSITIONS`
8. `MAX_LEVERAGE`
9. sizing: `POSITION_SIZE`, `INSUFFICIENT_BALANCE`, `EXCHANGE_PRECISION`,
   `MIN_NOTIONAL`, `PORTFOLIO_EXPOSURE`

Account-level checks come before sizing **by construction**. That ordering was
bought with a real phase 6 defect: an emergency stop was masked by an
incidental step-size problem because sizing ran first, and the reported code
sent a reader looking in the wrong place entirely.

Phase 7 introduces **no new rejection codes**. Two that had been defined since
phase 0 and never fired are now wired: `COOLDOWN` and `ABNORMAL_VOLATILITY`.

### Deriving the leverage ceiling

Two independent constraints, tighter wins, both from measured quantities:

**Liquidation must sit outside the stop.** At leverage *L* the position is
liquidated on a 1/*L* adverse move. If the stop is *s*% away and 1/*L* ≤ *s*/100,
the position is liquidated before the stop can fire and the stop is decoration.
So *L* < 100/*s*, with a safety factor of 2 because the liquidation model is
optimistic (loss equal to posted margin; real venues liquidate earlier).

**A stop-out must fit the day's remaining loss budget.** Loss at the stop is
`margin × L × s/100`; requiring that to stay within what is left of the daily
loss limit bounds *L* again. This is the constraint that tightens as a bad day
progresses, which is what a daily limit is for.

Both need a stop distance. Without one there is no derivation and the answer is
`None` — which the chain reports as `RISK_MAX_LEVERAGE_UNKNOWN`. It never means
"unlimited", and there is no fallback.

### Nothing a strategy thinks can loosen the envelope

Bias and condition counts ride on the proposal **for the audit trail only**.
The engine never reads them, and a parametrised test asserts that a
maximally-agreeing proposal and a maximally-disagreeing one produce byte-identical
verdicts. A rule that let an analysis's own reading relax the limit it is being
judged against would not be a limit.

## 3. The autonomous loop

A single `asyncio` task inside the FastAPI process, started by the lifespan
handler. Not a separate worker: paper state is in-memory, so a second process
would trade a different account.

### It is off

Three independent conditions, all required:

1. `AETHERIS_AUTONOMOUS_TRADING_ENABLED=true` — gates *availability*. With it
   false the arm endpoint refuses and the task is never created.
2. Paper mode enabled.
3. A deliberate `POST /api/v1/paper/autonomous {"enabled": true}`.

**Configuration alone never starts it**, and the runtime arm does not survive a
restart. A process that crashed and came back trading unattended, against an
account it does not remember, is the worst outcome available here.

### The universe is never chosen for you

`AETHERIS_AUTO_SYMBOLS` is **empty by default**. An armed loop with no symbols
evaluates nothing and records why. It does not fall back to BTC/ETH, to the
most liquid instruments, or to a scanner ranking.

The Market Opportunity Score is deliberately **not** used to select trades. It
is a ranking metric for where to look, and using it to pick positions would
edge toward exactly the predictive reading the spec forbids describing it as.

### One iteration

```
1. manage open positions      (tick: stops, targets, trailing, liquidation)
2. per symbol: evaluate the strategy on closed bars
3. flat + directional bias    → propose an entry
4. RISK ENGINE                → approve with a size, or refuse with a code
5. paper gate                 → checks again, independently
6. record one decision per symbol, always
```

Management runs **before** entry: a position that should have closed must not
still occupy a slot when the entry check counts open positions.

### Entry and exit

| Reading | Action |
|---|---|
| status not `READY` | no action, status recorded |
| `NEUTRAL` | `NO_SIGNAL` — a finding, not a gap |
| `LONG_BIAS` / `SHORT_BIAS` | propose an entry |
| bias opposes an open position | `SIGNAL_FLIP` close |
| bias goes `NEUTRAL` while open | **no close** |

Neutral means the conditions no longer align, not that they oppose. Closing on
neutral would churn the account on every indecisive bar.

### Sizing

`margin = available_balance × AETHERIS_AUTO_POSITION_SIZE_PERCENT / 100`,
default 10%, measured against *available* balance. Unrealised profit does not
fund new positions.

The result then passes the venue's real published filters unchanged. On a
100 USDT account that means most instruments are refused with
`EXCHANGE_PRECISION` or `MIN_NOTIONAL` — BTCUSDT's step is 0.001 BTC (~80 USDT
per increment) and ETHUSDT publishes a 20 USDT minimum notional. **Those are
Binance's constraints honoured, not the engine being timid**, and the fix is to
choose instruments the balance can trade or to raise the balance — never to
loosen the filter.

## 4. Idempotency

```
client_order_id = auto-{SYMBOL}-{timeframe}-{bar_close_ms}-{SIDE}
```

Duplicate protection is structural rather than a flag:

- a retry after a transient failure recomputes the same key and replays the
  original outcome, opening nothing new;
- **at most one autonomous entry per symbol per closed bar** falls out of the
  key for free, so re-evaluating the same 15-minute candle every 30 seconds
  cannot open a second position;
- refusals are stored under their key too, so a refused bar is not retried
  every iteration.

The bar's close time comes from the **candle series, not the clock**, which is
what makes the key stable across iterations within a bar.

## 5. Unusable data

Unchanged in policy: refuse, never invent. What phase 7 adds is loop behaviour.

- A symbol with unusable data is **skipped for the iteration**, recorded, and
  does not abort the others.
- An unmarkable open position is left alone with `null` PnL. It is **not**
  closed — closing at an invented price is the failure this rule prevents.
- After `AETHERIS_AUTO_MAX_CONSECUTIVE_FAILURES` (10) a symbol is dropped for
  the session and stops costing quota.
- When every symbol fails for `AETHERIS_AUTO_OUTAGE_ITERATIONS` (5) iterations
  the loop reports a venue outage and lengthens its interval. It does not stop:
  an outage is not a reason to abandon open positions.

## 6. Emergency stop and daily limits

Both unchanged from phase 6, and not reimplemented — the loop proposes and the
existing machinery refuses, with the same codes.

- A lock stops **opening only**. The loop keeps managing open positions while
  locked; stops and targets continue to fire. A loop that stopped managing when
  locked would leave positions unattended at exactly the wrong moment.
- Engaging the emergency stop **also disarms autonomy**. Releasing it does
  **not** re-arm — that is a separate deliberate act.
- Locks reset at the UTC day boundary.

## 7. Failing loudly

If the task raises, the supervisor records the exception, **disarms autonomy,
and does not restart**. A loop that silently restarts after an unexplained
crash hides the crash. The status reports `FAILED` with the exception type and
message, and the terminal shows it as a banner rather than letting a dead loop
look like a quiet market.

Re-arming clears the failure, because re-arming is a human saying they have
looked at it.

## 8. The audit trail

**Every iteration records one decision per symbol it looked at, including the
ones where nothing happened.** "The loop did nothing for six hours" has to be
distinguishable from "the loop was not running", and both from "the loop died
quietly" — a log of only the interesting rows cannot do that.

Each decision carries the action, the strategy status and bias, the condition
**counts**, the rejection code and its sentence, the full leverage chain, the
named checks the risk engine performed *including the ones that passed*, the
links into paper state, and the provenance of the data it read.

Bounded at `AETHERIS_AUTO_MAX_DECISION_LOG` (500), in memory, lost on restart
like all paper state.

`GET /api/v1/paper/autonomous/decisions?limit=N` — newest first.

## 9. Nothing is predicted

| Never | Instead |
|---|---|
| A price when data is unusable | refusal; `null` marks |
| A confidence or probability | condition counts (`3/4`), as counts |
| A win rate from the loop | realised trade history, labelled as history |
| An ML prediction | none exists; `ai.analysis` stays `PLANNED` |
| A signal-strength scalar | the named conditions and whether each held |
| A venue leverage ceiling | `null`, always |

A source-level test bans *confidence*, *probability of profit*, *win
probability*, *win rate*, *expected return*, *prediction*, *forecast*,
*AI-selected* and *guaranteed* from the phase 7 modules unless the surrounding
text is denying them. Comments and docstrings are included, because a docstring
that calls a condition count a confidence is how the misreading spreads — into
an API description, then a UI string, then a screenshot.

## 10. Separation from testnet and live

Three structural barriers, each asserted by a test:

1. **The loop depends on `PaperEngine` concretely.** There is deliberately no
   `ExecutionPort` a testnet adapter could later satisfy. Pointing it at a venue
   means writing new code and changing its type, not editing a config value.
2. **The mode is not a parameter.** No method takes a `TradingMode`, so there is
   no `mode=LIVE` to pass.
3. **The routes live under `/paper/`.** A future testnet loop needs its own
   route, its own switch and its own acknowledgement.

The loop has no code path to the testnet venue: it reaches `/paper` only.
Testnet execution exists as of phase 8b but is manual-only; `execution.live`
remains `PLANNED` and no adapter reports `LIVE`.

## 11. Concurrency

Phase 6 had one writer: the HTTP request. The loop is a second, and every
mutating path in `PaperTradingService` does a read-modify-write across an
`await`.

`PaperEngine`'s methods are synchronous, so an engine call is atomic within the
event loop and state cannot be corrupted. `_write_lock` adds that the **whole
fetch-then-mutate sequence** is atomic: a `reset` can no longer land between a
submission's price fetch and its fill, and an emergency stop cannot be observed
half-applied.

Reads do not take the lock. A snapshot one moment out of date is not a
correctness problem, and blocking reads behind writes would make the terminal
stutter whenever the loop is working.

Verified by removing the lock and confirming two tests fail
(`tests/unit/test_paper_concurrency.py`).

## 12. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AETHERIS_AUTONOMOUS_TRADING_ENABLED` | `false` | Gates arming; never arms |
| `AETHERIS_AUTO_SYMBOLS` | `[]` | **Empty.** Never chosen for you |
| `AETHERIS_AUTO_TIMEFRAME` | `15m` | Closed bars only |
| `AETHERIS_AUTO_INTERVAL_SECONDS` | `30` | Management latency, not entry rate |
| `AETHERIS_AUTO_MAX_SYMBOLS` | `5` | Per iteration |
| `AETHERIS_AUTO_POSITION_SIZE_PERCENT` | `10` | Of available balance |
| `AETHERIS_AUTO_REQUESTED_LEVERAGE` | `1` | A request; >1 fails closed |
| `AETHERIS_AUTO_STOP_LOSS_PERCENT` | `2` | |
| `AETHERIS_AUTO_TAKE_PROFIT_PERCENT` | `4` | |
| `AETHERIS_AUTO_TRAILING_STOP_PERCENT` | off | |
| `AETHERIS_AUTO_CLOSE_ON_SIGNAL_FLIP` | `true` | Never on neutral |
| `AETHERIS_AUTO_MAX_ATR_PERCENT` | `15` | Measured, not forecast |
| `AETHERIS_AUTO_MAX_CONSECUTIVE_FAILURES` | `10` | Then drop the symbol |
| `AETHERIS_AUTO_MAX_DECISION_LOG` | `500` | Bounded in memory |
| `AETHERIS_RISK_ENTRY_COOLDOWN_SECONDS` | `300` | Now enforced |

## 13. API

| Route | Method | Effect |
|---|---|---|
| `/api/v1/paper/autonomous` | GET | state, counters, universe, failure |
| `/api/v1/paper/autonomous/decisions` | GET | the bounded audit trail |
| `/api/v1/paper/autonomous` | POST | arm / disarm |

One new write route, under the existing `/paper` namespace, so
[ADR 0003](adr/0003-write-routes-and-the-get-only-invariant.md) needs no
amendment: writes exist only under `/paper`, against simulation state.

## 14. Verified against live Binance data

21 September 2026, five symbols on a 5-minute timeframe:

```
fresh process          → enabled=false, state=DISARMED  (config permitted, symbols set)
armed                  → state=ARMED
10 iterations          → 50 decisions recorded
ETHUSDT ADAUSDT DOGEUSDT XRPUSDT SOLUSDT
                       → NO_SIGNAL, NEUTRAL, 3/4 conditions, ATR 0.28-0.58%
ETHUSDT (position)     → MANAGED each iteration, no level fired
manual 3x              → RISK_REJECTED_MAX_LEVERAGE, approved=null
manual 1x              → FILLED 0.018 @ 2,724.49 best_ask
emergency stop         → entry refused RISK_REJECTED_EMERGENCY_STOP, position kept
unlisted symbol        → RISK_REJECTED_STALE_DATA
```

**No autonomous entry occurred during verification**, because every symbol on
every timeframe checked read NEUTRAL — a genuinely flat market, not a defect.
The entry and refusal paths are covered by the unit and integration suites
instead. That the loop ran for ten iterations and opened nothing, while saying
why each time, is the behaviour the audit trail exists to make visible.

## 15. Not in this phase

Portfolio accounting (`portfolio.engine`), order lifecycle and reconciliation
(`order.engine`), testnet and live execution, AI and Falcon. All remain
`PLANNED`. Ask the running service: `GET /api/v1/system/capabilities`.
