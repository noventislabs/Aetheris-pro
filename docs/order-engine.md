# Order engine (phases 8a-8b)

> Status: Phase 8b. The order **lifecycle** is built: a state machine,
> deterministic identity, and the reconciliation protocol. Records are durable
> in PostgreSQL, and `BinanceTestnetTradingAdapter` reaches the Binance Demo
> venue — the testnet path only, and nothing reports `LIVE`.
>
> **Crash recovery is delivered.** A recovery pass runs at startup over the
> durable records. It requires `DATABASE_URL`: without a configured database
> the order lifecycle is absent entirely and nothing claims crash recovery.

## 1. The scenario this exists for

```
order record written      ← a crash here is safe
venue submission          ← a crash HERE is the problem
venue response            ← a lost response is the same problem
```

Between the submission and the response, **local state cannot tell you whether
an order exists in the world.** Every wrong answer is expensive:

| Guess | Cost |
|---|---|
| "it failed" → resubmit | **two positions** |
| "it succeeded" → mark filled | **phantom position**, and the risk engine sizes the next order against margin that is not committed |
| "unknown" → keep trading | trading blind |

Phase 0 already wrote the answer into the `OrderState` docstring, a year of
phases before anything could use it:

> `UNKNOWN` and `RECONCILING` are not error states — they are the honest
> representation of "the exchange has not told us yet". The order engine must
> never collapse them into `FILLED` or `CANCELLED` by assumption.

Phase 8a makes that structural.

## 2. The state machine

```
CREATED ──> VALIDATING ──> SUBMITTED ──> ACCEPTED ──> PARTIALLY_FILLED ──> FILLED
   │            │              │            │               │
   │            ├──> REJECTED  ├──> REJECTED├──> CANCEL_REQUESTED ──> CANCELLED
   │            │   (risk)     │   (venue)  │                     └──> FILLED
   └──> CANCELLED              └──> EXPIRED └──> EXPIRED

any open state ──> UNKNOWN ──> RECONCILING ──> {ACCEPTED, PARTIALLY_FILLED,
                                                FILLED, CANCELLED, REJECTED,
                                                EXPIRED, UNKNOWN}
```

Three properties, each a test rather than a convention:

**`UNKNOWN` has exactly one exit, and it is `RECONCILING`.** There is no edge to
`FILLED`. There is no edge to `CANCELLED`. A timer cannot resolve an unknown
order; a default cannot; an optimistic assumption has nowhere to write itself.
Code that wants to settle an unknown order must first declare that it is asking,
and asking only produces a state from an answer.

**Submission requires validation.** `CREATED` cannot reach `SUBMITTED` — it must
pass through `VALIDATING`. The risk check is therefore a structural prerequisite
of sending an order rather than a call site someone could forget.

**Terminal is terminal.** `FILLED`, `CANCELLED`, `REJECTED` and `EXPIRED` have
no outgoing edges at all. A late venue message about a settled order is recorded
as a discrepancy and never applied: applying it would destroy both the evidence
and the correct state.

An illegal transition **raises**. A state machine that shrugs at an impossible
transition is one that will eventually mark a phantom order filled.

The whole machine is one table, `LEGAL_TRANSITIONS`, so the lifecycle can be
audited without reading the code that drives it.

## 3. Deterministic identity

The identity a venue echoes back is what recovery queries by, so it has one hard
requirement: **it must be derivable again after a restart.** A UUID or a counter
is lost with the process, and an order whose identity cannot be reconstructed is
an order that cannot be asked about.

```
client_order_id = "aeth-" + blake2b(account_id | intent_key, 10 bytes).hex()
                = 25 characters, always
```

**The venue limit is real and narrow: 36 characters over a restricted
alphabet.** The phase 7 paper key — `auto-ETHUSDT-15m-1789992000000-BUY` — is 33
characters and **overflows for longer symbols**. That would have surfaced at the
first real submission of an unusual instrument, which is the worst available
moment. A parametrised test now covers `1000000MOGUSDT` and friends.

The readable form is not lost: it lives on the record as `intent_key`
(`manual:ETHUSDT:BUY:bucket-1`), where nothing truncates it.

`bucket` must be **stable** — a candle close time, a caller-supplied key — never
a wall-clock reading. Re-deriving during recovery would otherwise produce a
different id and defeat the entire mechanism.

## 4. The reconciliation protocol

A pure decision function over every answer a venue can give, written and tested
in 8a so 8c attaches an adapter to a protocol that already works.

The asymmetry at the centre of it:

> **A venue saying "no such order" is only proof when we never sent one.**

| Record | Venue answer | Conclusion |
|---|---|---|
| never submitted | not found | **conclusive** — settle locally as `CANCELLED` |
| submitted | not found | **not proof** — stay `UNKNOWN`, keep blocking |
| any | found | the venue is the authority — adopt its state |
| terminal | disagrees | record a discrepancy, change nothing |
| any | fills went backwards | record a discrepancy, change nothing |
| any | unreachable | change nothing; an unanswered question is not an answer |

Treating the second row like the first — assuming absence means it never landed,
and resubmitting — is how one order becomes two positions.

`submitted_at` is stamped **before** the network call, never after it. A record
with `submitted_at` set and no `venue_order_id` is exactly what a crash
mid-flight leaves behind, and that pairing is what separates row one from row
two.

### What a recovery pass asks about

**Every non-terminal order, not only the explicitly unknown ones.** An order
believed `ACCEPTED` may have filled while the process was down, and assuming
otherwise is the same error as assuming an unknown order never landed.

### Manual resolution

An order the venue will never answer about would block entries forever, so a
human can settle it — narrowly and loudly. Only from `UNKNOWN` or `RECONCILING`,
only to a state reconciliation could have concluded, and always recorded with
who did it and why. A manual resolution that left no trace would be
indistinguishable from the inference this design forbids.

## 5. `RISK_REJECTED_RECONCILIATION_PENDING`

Defined in phase 0. Never fired until now.

**While any order is unreconciled, no new entry is permitted.** `UNKNOWN` and
`RECONCILING` both mean there may be a position at a venue that this system
cannot see, and sizing the next order against that is how a small outage becomes
a large loss.

It sits **before the daily lock** in the risk engine's authority order, and that
placement is deliberate: if orders are unreconciled then the day's realised PnL
may itself be wrong — a fill this system has not seen is a fill not in the total
— so judging the daily budget first would mean judging it against numbers
already known to be incomplete.

Order of authority:

```
mode → emergency stop → RECONCILIATION PENDING → daily lock → data quality
     → cooldown → volatility → slots → leverage → sizing
```

**Paper is unaffected.** `unreconciled_orders` defaults to zero and paper has no
venue to be out of step with, so phase 6 and 7 behaviour is unchanged.

## 6. What 8a did not do, and what 8b since delivered

| Item | Status now |
|---|---|
| Crash recovery | **Delivered in 8b.** A startup pass runs over durable records. Requires `DATABASE_URL`. |
| Durable order records | **Delivered in 8b**, in PostgreSQL, with identity unique by constraint. |
| Any venue submission | **Delivered in 8b, testnet only**, via `BinanceTestnetTradingAdapter`. No `LIVE` adapter exists. |
| Credentials | Binance **Demo** credentials, server-side only, for the testnet path. |
| Paper adopting this machine | Deliberately not: a simulated fill is instantaneous, and touching working phase 6 code for no behavioural gain is not worth the risk. The domain types are shared; the lifecycle is not. |

## 6a. Gaps found in the 8a review, and left open deliberately

One defect was found and fixed during review; three gaps were found and left
for the phase that can actually close them.

**Fixed — the average price did not move with the quantity.** Adopting a venue
total while keeping the locally computed average produced a record whose implied
notional was a number nothing observed: local 0.6 @ 100 adopting a venue total
of 1.0 @ 105 left the record claiming 1.0 @ **100**. A fabricated price arrived
at by omission rather than invention, which is the harder kind to notice. The
decision now carries the venue's average alongside its quantity, and when the
venue supplies no average the field is set to `None` -- unknown, not stale.
Three regression tests.

**Open — no submission retry state.** `reconciliation_attempts` counts the
retries that exist in 8a. There is no counter or backoff for *submission*
attempts, because 8a has nothing to submit to. Phase 8c needs it, and it needs
to be durable, or a retry storm survives a restart invisibly.

**Open — no lookup by `venue_order_id`.** Recovery queries by `client_order_id`,
which is the point of derivable identity and is sufficient for REST polling. A
venue *push* (websocket order stream) arrives keyed by the venue's id and would
need a second index. Not added speculatively; the repository port is
deliberately narrow.

**Open — the engine is synchronous, and 8c will make it not.** Every method is
sync, so each is atomic within the event loop and no lock is needed today. Phase
8c introduces an `await` between `mark_submitting` and `apply_venue_ack` -- the
network call -- which opens the same read-modify-write window the paper service
closed with `_write_lock`. It must be closed the same way, with a per-order
single-flight rather than a global lock, or two reconciliation passes will race
on one order.

## 7. Layering

```
engines/order/
  machine.py      the transition table, applied or refused   (pure)
  identity.py     deterministic client order ids             (pure)
  reconcile.py    the decision over every venue answer        (pure)
  store.py        OrderRepository port + in-memory impl
  engine.py       lifecycle orchestration over the port
```

Pure in the same sense as `engines/risk/`: no HTTP, no framework, no venue, no
clock of its own — `now` is always passed in, so a test can put an order through
a crash window without waiting for one. The architecture test enforces it.

The repository port is the seam phase 8b fills, exactly as `PaperRepository` was
for phase 6. What 8b must provide is listed in
[phase-8-design.md](phase-8-design.md) §5: `NUMERIC` money columns,
`timestamptz`, row-level locking, and a unique constraint on the client order id
so idempotency is enforced by the database rather than only by application code.

## 8. Tests

123 covering: every legal and illegal transition; the two forbidden inferences
by name; terminal states having no exits; submission requiring validation;
identity determinism, separation and venue bounds for long symbols; the crash
window in both directions; the asymmetry of "not found"; discrepancies recorded
and not applied; fills monotonic; manual resolution and its refusal; and
`RECONCILIATION_PENDING` blocking entry and outranking the daily lock.
