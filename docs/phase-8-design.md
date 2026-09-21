# Phase 8 — Order Engine and Testnet Execution

> **Design only. No code exists for this phase.** Nothing here is implemented
> and nothing should be until it is agreed. Every capability named stays
> `PLANNED` in the registry until its tests land.
>
> This is the phase that crosses from simulation to a real matching engine. It
> is the first code in the project that holds a credential and the first that
> can have an effect nobody can undo by restarting the process.

## 0. What changed before this design was written

You resolved [ADR 0002](adr/0002-database-hosting.md): **hosted PostgreSQL free
tier**. That unblocks Phase 1 (authentication, persistence, migrations), which
has blocked since Phase 0 — and it changes Phase 8's shape, because half of
Phase 8 was waiting on exactly that.

---

## 1. The sequencing problem, stated first

Phase 8 is two capabilities:

| Capability | Registry text |
|---|---|
| `order.engine` | "Idempotent submission and **crash recovery**." |
| `execution.testnet` | "**Isolated credentials** and order records." |

Both phrases point at durable state. **Crash recovery over in-memory state
recovers nothing** — there is nothing to recover from, because the crash took
the state with it. An order engine whose records vanish on restart cannot
reconcile, and reconciliation is the entire point of an order engine.

So Phase 8 splits along a real seam:

| Part | Needs | Buildable |
|---|---|---|
| **8a — Order lifecycle** | nothing | **now** |
| **8b — Durable order store** | Phase 1's database | after Phase 1 |
| **8c — Testnet execution** | 8b + your testnet credentials | after 8b |

**Recommendation: Phase 1 lands between 8a and 8b.** 8a is pure — a state
machine, transition rules, idempotency keys, the reconciliation protocol — and
is a strict prerequisite for testnet *and* live regardless of what happens
next. It can start immediately. But 8c must not ship before 8b, because a
testnet order whose record is lost on restart is an order you cannot reconcile,
and an unreconcilable real order is worse than no order at all.

**This is the recommendation I most want a decision on (§20.1).**

---

## 2. The most dangerous moment in this system

Everything in 8a exists to survive one scenario:

```
1. risk engine approves
2. order record written                    <- if the process dies here: safe
3. HTTP POST to the venue                  <- if the process dies HERE: ???
4. venue responds "accepted, id 12345"     <- if this response is lost: ???
5. order record updated
```

Between 3 and 5 **you do not know whether an order exists.** The process
crashed, the network dropped, the venue timed out. There is a position in the
world or there is not, and local state cannot tell you which.

Every wrong answer here is expensive:

- Assume it failed, resubmit → **two positions**.
- Assume it succeeded, mark it filled → **phantom position**, and the risk
  engine sizes the next order against margin that is not committed.
- Mark it `UNKNOWN` and keep trading → trading blind.

The correct answer is the one Phase 0 already wrote into the enum docstring:

> `UNKNOWN` and `RECONCILING` are not error states — they are the honest
> representation of "the exchange has not told us yet". The order engine must
> never collapse them into `FILLED` or `CANCELLED` by assumption.

So: **write the intent before submitting, submit with a deterministic client
order id, and on recovery ask the venue what happened to that id.** Never
infer. That is why 8c needs 8b — step 2 must survive the crash, or step 4 has
nothing to reconcile against.

---

## 3. The order state machine (8a)

`OrderState` has been a vocabulary since Phase 0 and has never been a machine.
Phase 8 makes the transitions explicit and tested.

```
CREATED ──> VALIDATING ──> SUBMITTED ──> ACCEPTED ──> PARTIALLY_FILLED ──> FILLED
   │            │              │            │               │
   │            ├──> REJECTED  │            ├──> CANCEL_REQUESTED ──> CANCELLED
   │            │  (risk)      ├──> REJECTED│                     └──> FILLED
   │            │              │  (venue)   ├──> EXPIRED
   └──> CANCELLED (before submit)           └──> CANCELLED

any open state ──> UNKNOWN ──> RECONCILING ──> {ACCEPTED, PARTIALLY_FILLED,
                                                FILLED, CANCELLED, REJECTED,
                                                EXPIRED, UNKNOWN}
```

Rules the machine enforces, each a test:

1. **Terminal is terminal.** `FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED` have
   no outgoing edges. A late venue message about a terminal order is recorded
   as a discrepancy, never applied.
2. **No skipping.** `CREATED` cannot jump to `FILLED`. Every order that reached
   a venue passed through `SUBMITTED`.
3. **`UNKNOWN` resolves only through `RECONCILING`**, and `RECONCILING` resolves
   only from a venue answer — never from a timer, never from a default.
4. **`RECONCILING` may resolve back to `UNKNOWN`.** If the venue cannot answer,
   the honest outcome is still not knowing. This edge existing is the point.
5. **Fills only increase.** `filled_quantity` is monotonic; a venue message
   reducing it is a discrepancy.
6. **`PARTIALLY_FILLED` → `CANCELLED` keeps its fills.** A cancelled partial is
   not a cancelled order; the filled part is real.

An illegal transition raises rather than being silently ignored. A state
machine that shrugs at an impossible transition is a state machine that will
one day mark a phantom order filled.

### Where it lives

`engines/order/` — pure, joining `engines/risk/` under the same architecture
test. No HTTP, no framework, no venue.

```
engines/order/
  machine.py      legal transitions, applied or refused (pure)
  identity.py     deterministic client order ids (pure)
  reconcile.py    the reconciliation protocol (pure decision function)
  engine.py       lifecycle orchestration over a repository port
```

---

## 4. Idempotent submission (8a)

Phase 6 and 7 already proved this mechanism on paper; Phase 8 makes it the
thing that stops a duplicate *real* order.

```
client_order_id = deterministic from (account, symbol, intent, bar/time bucket)
```

Requirements the venue imposes and the design must respect:

- **Binance caps `newClientOrderId` at 36 characters** and restricts the
  alphabet. The Phase 7 key (`auto-SYMBOL-tf-epochms-SIDE`) can exceed that for
  long symbols, so 8a needs a bounded encoding — a short prefix plus a hash,
  with the readable form kept in our own record rather than in the venue field.
- The id must be **stable across a retry and across a restart**, which means it
  is derived, not generated — no UUIDs, no counters that reset with the process.
- The id is written to our store **before** the submission leaves, so a crash
  mid-flight leaves a record that recovery can query by.

**Recovery protocol** on startup, for every non-terminal order:

```
for each order not in a terminal state:
    ask the venue: GET order by client_order_id
      found      → apply the venue's state (it is the authority)
      not found  → was it ever submitted?
                     record says SUBMITTED  → UNKNOWN, needs a human or a retry
                     record says CREATED    → never sent; safe to cancel locally
      venue down → RECONCILING, and block new entries
```

`RISK_REJECTED_RECONCILIATION_PENDING` already exists in the vocabulary from
Phase 0 and has never fired. This is what it is for: **while any order is
unreconciled, no new entry is permitted.** Trading on top of an unknown
position is how a small outage becomes a large loss.

---

## 5. Durable order records (8b)

Needs Phase 1. What Phase 8 requires *from* Phase 1:

| Requirement | Why |
|---|---|
| `NUMERIC` for every money column | The decimal discipline must survive the round trip. No `float`, no `double precision`. |
| `timestamptz` everywhere | Order timing across a restart is UTC or it is wrong. |
| Row-level locking (`SELECT … FOR UPDATE`) | Two workers must not reconcile the same order at once. |
| Unique constraint on `(account, client_order_id)` | Idempotency enforced by the database, not only by application logic. |
| Migrations | Order tables change; an order history that cannot migrate is an order history that gets dropped. |

These are exactly the properties ADR 0002 warned SQLite diverges on, which is
why the hosted-PostgreSQL decision matters here specifically.

**The repository seam already exists.** Phase 6 built `PaperRepository` as an
interface precisely so a durable implementation slots in without reshaping the
engine. 8b follows the same shape: an `OrderRepository` port, an in-memory
implementation for tests, a PostgreSQL implementation for real use.

---

## 6. Testnet execution (8c)

### The credential boundary

This is the first credential in the project. The rules are not negotiable and
come from the master spec:

- **Never** in the frontend, browser storage, source, Git, logs or URLs.
- **Trading permissions only. Withdrawal permissions must never be required**,
  and the design should make requiring them impossible rather than merely
  discouraged.
- Testnet credentials live in their own settings class with their own env
  prefix (`AETHERIS_TESTNET_*`), never sharing a field with live.

### Structural separation from live

A testnet key must be incapable of reaching the live venue by configuration
alone:

1. **Separate settings class**, separate base URL, no shared field.
2. **The base URL is not derived from a flag.** `TestnetTradingSettings` carries
   `testnet.binancefuture.com` as its own validated value; there is no
   `if live: url = ...` branch that a typo can flip.
3. **Separate adapter class.** `BinanceTestnetTradingAdapter` implements
   `TradingPort`; live gets its own class in Phase 10. One adapter with a mode
   switch would be one typo away from the wrong venue.
4. **`live_trading_enabled` remains false and double-gated**, untouched by this
   phase.

### What becomes true for the first time

**`exchange_max_leverage` becomes knowable.** Testnet credentials grant access
to the venue's leverage brackets, so the constraint chain can complete for the
first time since Phase 4:

```
approved = min(requested, exchange_max, risk_max)   -- all three known
```

On testnet, **leverage above 1x becomes approvable.** That is the intended
consequence of the chain, not a loophole — but it is a genuine behavioural
change and it must not leak into paper mode. Paper holds no credential, so
paper's `exchange_max` stays null and paper stays at 1x. The design must make
that asymmetry explicit rather than letting it be discovered.

The configured `max_leverage` (default 3x) still binds, and the risk engine's
derived ceiling still binds. 500x remains a candidate range, never a target.

### What testnet does NOT get

- **No autonomous trading.** The Phase 7 loop stays paper-only. Pointing
  autonomy at a real matching engine is its own decision with its own review,
  not a side effect of this phase. The Phase 7 barriers (no `ExecutionPort`, no
  `TradingMode` parameter, routes under `/paper/`) stay exactly as they are.
- **No withdrawal capability**, in any form.
- **No live path.**

---

## 7. The risk engine is unchanged

This is Phase 7's payoff. `engines/risk/` is mode-independent by construction:
it rules on a `RiskProposal` against a `RiskAccountView` and a `RiskMarketView`
and knows nothing about where the order goes.

Phase 8 therefore adds **no risk logic**. It adds a second way to build the
account view — from venue balances and positions instead of paper state — and
the same `evaluate` call rules on it.

Two additions to the existing vocabulary, both already defined and unused:

- `RECONCILIATION_PENDING` — blocks entry while any order is unreconciled.
- `MODE_NOT_ENABLED` — already used; now gates testnet as well as paper.

**No new rejection codes.** If Phase 8 needs one, that is a signal the design
is wrong somewhere.

---

## 8. Route surface

New reads under a new namespace, and writes that follow
[ADR 0003](adr/0003-write-routes-and-the-get-only-invariant.md)'s pattern:

| Route | Method | Effect |
|---|---|---|
| `/api/v1/testnet/status` | GET | credential presence (never the value), connectivity |
| `/api/v1/testnet/account` | GET | venue balance and positions |
| `/api/v1/testnet/orders` | GET | order records and their states |
| `/api/v1/testnet/reconciliation` | GET | real status, not `NOT_APPLICABLE` |
| `POST /api/v1/testnet/orders` | POST | submit — **a real order** |
| `POST /api/v1/testnet/orders/{id}/cancel` | POST | cancel |
| `POST /api/v1/testnet/reconcile` | POST | force a reconciliation pass |

ADR 0003's invariant needs **amending**, not just extending: it currently says
writes exist only under `/paper`. A new ADR (0005) should record why `/testnet`
joins it and what the boundary now is. The stronger half — *no route can reach a
venue order endpoint* — becomes false by design here, and its replacement must
be written down deliberately: **only the testnet adapter may reach a venue order
endpoint, only under `/testnet`, only with testnet credentials, and the
architecture test asserts the live host appears nowhere.**

---

## 9. What must never happen in this phase

| Never | Enforcement |
|---|---|
| Live order placement | No live adapter; `live_trading_enabled` untouched and double-gated |
| Withdrawal | No withdrawal endpoint, no `withdraw` identifier; asserted at source |
| Credentials in the frontend | The terminal never receives a key; status reports presence only |
| Credentials in logs | The existing logging test extends to the new settings |
| Autonomous testnet trading | Loop stays paper-only; Phase 7 barriers unchanged |
| Collapsing `UNKNOWN` into a guess | State machine has no such edge; tested |
| Trading while unreconciled | `RECONCILIATION_PENDING` blocks entry |
| 500x | Candidate range only; configured and derived ceilings both bind |

---

## 10. Assumptions

1. **Phase 1 lands before 8b.** Hosted PostgreSQL provisioned, migrations
   running, `DATABASE_URL` supplied as a secret.
2. **You will create Binance testnet API keys** at `testnet.binancefuture.com`
   with **trading permission only**. I cannot create these.
3. **Testnet API shape matches production USDT-M Futures**, differing only in
   host. Believed true; verified by a smoke test before 8c is called done.
4. **Testnet balances are venue-granted play money**, not something this system
   funds or manages.
5. **One account, still.** Multi-tenancy arrives with Phase 1's auth; until
   then, one server-side account as in Phase 6.
6. **REST only.** No websocket order stream; reconciliation is poll-driven, as
   everything else in this build is.

---

## 11. Unresolved decisions

### 11.1 Sequencing — the one I want a decision on

Recommended: **8a now → Phase 1 → 8b → 8c.** The alternative is to do Phase 1
first in full and then Phase 8 end to end, which is cleaner but leaves Phase 8
untouched for longer.

### 11.2 Does the paper engine adopt the new order state machine?

Paper currently records `FILLED` or `REJECTED` and nothing between, because a
simulated fill is instantaneous. Adopting the machine would unify the model at
the cost of touching working Phase 6 code for no behavioural gain.
**Recommended: no** — leave paper alone, share the domain types only.

### 11.3 Where do testnet credentials live?

Environment variables via `.env` (gitignored) for development, matching every
other secret in this build. The alternative — encrypted at rest in the
database — is better practice and belongs with Phase 1's auth work, not here.
**Recommended: env for 8c, revisit when auth lands.**

### 11.4 Reconciliation cadence

Recommended: on startup, before any entry is permitted; then on demand via the
endpoint; then on a slow timer (5 min). Not on every request — reconciliation
costs venue quota and a tight loop turns an outage into a rate-limit ban.

### 11.5 Does testnet get a terminal page?

Recommended: yes, but read-heavy — order records, states, reconciliation status.
Order *entry* from the browser can wait until the flow is proven by API.

---

## 12. Failure modes

| # | Failure | Response |
|---|---|---|
| 1 | Crash between submit and response | Record written first; recovery queries by client id; `UNKNOWN` until answered |
| 2 | Venue returns a duplicate-id error | Treated as confirmation the order exists; query and adopt its state |
| 3 | Venue unreachable at startup | `RECONCILING`; all entry blocked with `RECONCILIATION_PENDING` |
| 4 | Late message about a terminal order | Recorded as a discrepancy; never applied |
| 5 | Fill quantity decreases | Discrepancy; monotonicity is enforced |
| 6 | Two workers reconcile one order | Row lock; single-flight per order |
| 7 | Clock skew vs venue timestamps | Venue time is authoritative for order events, as it already is for candles |
| 8 | Credential invalid or expired | Refuse at startup with a named error; never retry silently |
| 9 | Credential accidentally has withdrawal permission | Detect and **refuse to start**; do not merely warn |
| 10 | Testnet symbol not on live, or vice versa | Metadata comes from the venue actually being used |
| 11 | Database unavailable mid-session | Entry blocked; open orders reconciled when it returns |
| 12 | Partial fill then cancel | Fills retained; position reflects the filled part only |

---

## 13. Security boundaries

1. **One credential, one purpose.** Testnet keys in their own settings class,
   own prefix, own adapter, own base URL.
2. **Presence, never value.** The status endpoint reports whether a credential
   is configured. It never returns it, and `SecretStr` prevents accidental
   repr/log leakage.
3. **Withdrawal is checked, not assumed.** If the venue reports the key has
   withdrawal permission, the system refuses to start. A key with more power
   than it needs is a key that will eventually be used with it.
4. **No credential crosses to the browser.** The terminal calls our API; our API
   holds the key.
5. **Live remains double-gated and untouched.**
6. **The database URL is a secret**, already modelled as `SecretStr` and already
   required in production by `_production_requires_secrets`.
7. **Rate limiting.** Order endpoints have tighter venue limits than market
   data; the existing backoff applies, and reconciliation is bounded.

---

## 14. Tests required

**State machine (pure)** — every legal transition; every illegal one raises;
terminal states have no exits; `UNKNOWN` resolves only via `RECONCILING`;
`RECONCILING` may return to `UNKNOWN`; fills monotonic; partial-then-cancel
retains fills.

**Identity** — deterministic across restart; within Binance's 36-char and
alphabet limits for long symbols; distinct per account/symbol/side/bucket.

**Reconciliation** — venue found/not-found/unreachable each produce the right
state; a crash mid-flight leaves a queryable record; entry blocked while
pending; late terminal message recorded as a discrepancy, not applied.

**Persistence (8b)** — `NUMERIC` round-trips a Decimal exactly; `timestamptz`
round-trips UTC; the unique constraint rejects a duplicate client id;
concurrent reconciliation is single-flight.

**Testnet adapter (8c, mocked transport)** — submit/cancel/query shapes; venue
rejection surfaces with its code; duplicate-id treated as confirmation;
credential absent refuses cleanly; withdrawal-capable key refuses to start.

**Architecture** — `engines/order/` pure; live host appears nowhere; testnet
adapter is the only `TradingPort` implementation; autonomy still cannot reach
it; no credential in logs; no withdrawal identifier anywhere.

**Risk** — the same `evaluate` rules on a venue-derived account view; `>1x`
approvable on testnet once `exchange_max` is real; paper unchanged at 1x.

**Live smoke (8c)** — against real testnet with real keys: submit, query,
cancel, reconcile, and a deliberate mid-flight interruption.

---

## 15. Files and modules

**New**
```
backend/src/aetheris/engines/order/{machine,identity,reconcile,engine}.py
backend/src/aetheris/domain/order.py
backend/src/aetheris/adapters/exchange/binance/testnet_adapter.py       (8c)
backend/src/aetheris/adapters/persistence/                              (8b, phase 1)
backend/src/aetheris/services/testnet.py                                (8c)
backend/src/aetheris/api/v1/testnet.py                                  (8c)
docs/adr/0005-testnet-write-surface.md
docs/order-engine.md · docs/testnet.md
```

**Modified**: `core/config.py` (`TestnetTradingSettings`), `core/capabilities.py`,
`adapters/exchange/ports.py` (`TradingPort` gains real signatures),
`api/v1/router.py`, `main.py`, `tests/unit/test_architecture.py` (the
`TradingPort`-has-no-implementation assertion **changes meaning** and must be
rewritten deliberately, not deleted).

---

## 16. What I need from you

1. **§11.1 sequencing** — 8a now, then Phase 1, then 8b/8c? Or Phase 1 in full first?
2. **Testnet API keys** — will you create them at `testnet.binancefuture.com`,
   trading permission only? 8c cannot be verified without them.
3. **Hosted PostgreSQL** — you chose the free tier; provisioning needs your
   signup. Once you have a connection string, Phase 1 can start.

Everything else in §11 has a recommendation I am content to proceed on.
