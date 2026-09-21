# Phase 1 — exactly what Phase 8b needs from the database

> **Specification only. Nothing here is implemented.** Phase 1 has not started
> and cannot start until `DATABASE_URL` exists.
> [ADR 0002](adr/0002-database-hosting.md) is accepted (hosted PostgreSQL free
> tier); **provisioning is outstanding and needs the maintainer.**
>
> This document is deliberately narrow: not "what should Phase 1 contain", but
> "what must Phase 1 deliver before Phase 8b can honestly claim crash recovery".

---

## 1. The dependency in one line

> **Crash recovery over in-memory order state is not degraded. It is
> impossible** — the thing a recovery pass queries is exactly the thing a
> restart destroys.

And it fails *silently*, which is worse than failing loudly. With an empty
store `unreconciled_orders == 0`, so `RISK_REJECTED_RECONCILIATION_PENDING`
does not fire and trading resumes immediately on top of a venue position nobody
is tracking. The safety mechanism is inert precisely when it is needed.

There is no in-memory arrangement that fixes this, and none should be
attempted.

---

## 2. Schema

Four tables. Money is `NUMERIC`, time is `timestamptz`, and neither is
negotiable — see §7.

### `accounts`
| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `owner_id` | `uuid` FK → `users` | **Every query filters on this.** §6 |
| `mode` | `text` | `PAPER` · `TESTNET` · `LIVE` |
| `starting_balance` | `NUMERIC(24,8)` | |
| `created_at` | `timestamptz` | |

A `(owner_id, mode)` unique constraint gives one account per user per mode and
makes the phase 6 "exactly one account" property a database fact rather than a
convention.

### `orders`
| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `account_id` | `uuid` FK | |
| `client_order_id` | `text` | **`UNIQUE (account_id, client_order_id)`** — §4 |
| `venue_order_id` | `text` NULL | Null until acknowledged; the gap *is* the uncertainty window |
| `state` | `text` | Constrained to the `OrderState` vocabulary |
| `symbol`, `side`, `order_type` | `text` | |
| `quantity`, `price`, `filled_quantity`, `average_fill_price` | `NUMERIC(24,8)` | `price` and `average_fill_price` nullable |
| `intent_key` | `text` | The readable form the identity derives from |
| `origin`, `mode` | `text` | |
| `created_at`, `updated_at` | `timestamptz` | |
| `submitted_at` | `timestamptz` NULL | **Written before the network call.** §5 |
| `terminal_at` | `timestamptz` NULL | |
| `rejection_code`, `rejection_detail`, `venue_rejection` | `text` NULL | |
| `reconciliation_attempts` | `integer` | |
| `last_reconciled_at` | `timestamptz` NULL | |
| `reconciliation_detail` | `text` NULL | |
| `submission_attempts` | `integer` | **New in 8b** — §8 |
| `next_retry_at` | `timestamptz` NULL | **New in 8b** — §8 |
| `resolved_by_operator`, `operator_reason` | `text` NULL | Manual settlement, always attributed |

Indexes: `(account_id, state)` for the recovery sweep; `(account_id,
client_order_id)` unique; `(venue_order_id)` for a future push stream (§8).

### `order_fills`
| Column | Type |
|---|---|
| `id` `uuid` PK · `order_id` `uuid` FK · `venue_trade_id` `text` NULL |
| `price`, `quantity`, `fee` `NUMERIC(24,8)` · `filled_at` `timestamptz` |

`UNIQUE (order_id, venue_trade_id)` where the venue supplies one — a replayed
venue message must not double-count a fill.

### `order_discrepancies`
| Column | Type |
|---|---|
| `id` · `order_id` · `observed_at` `timestamptz` · `detail` `text` · `local_state` `text` · `venue_state` `text` NULL |

Append-only. This is evidence; nothing updates or deletes a row here.

---

## 3. Migrations

Alembic, `backend/migrations/`. Two requirements beyond the obvious:

- **Every migration is reversible or explicitly marked irreversible.** An order
  history that cannot migrate is an order history that gets dropped.
- **No migration rewrites a money column's type in place.** Add, backfill,
  verify, then drop — the standard dance, but it matters more here than usual.

---

## 4. Idempotency persistence

The application already enforces one identity per intent
(`DuplicateClientOrderIdError`). That guarantee must move into the database:

```sql
UNIQUE (account_id, client_order_id)
```

**Why it cannot stay in application code:** phase 8b's recovery pass and a live
request can run concurrently, and two processes could both check-then-insert.
A dictionary cannot serialise across processes; a unique constraint can. The
repository catches the integrity error and treats it as *confirmation the order
already exists* rather than as a failure.

---

## 5. Transaction boundaries

Three, and they are the whole of what makes recovery work.

**A. Create → submit.** The order row is committed **before** the network call,
with `submitted_at` set in the same transaction as the `SUBMITTED` state. A
crash after commit and before the response leaves exactly the signature
recovery needs: `submitted_at` present, `venue_order_id` null.

```
BEGIN; INSERT order (CREATED); COMMIT;
BEGIN; UPDATE state=VALIDATING; COMMIT;          -- risk gate runs here
BEGIN; UPDATE state=SUBMITTED, submitted_at=now; COMMIT;
        ── network call ──                        ← crash window
BEGIN; UPDATE state=ACCEPTED, venue_order_id=…; COMMIT;
```

Committing the submission stamp *with* the state change, in one transaction, is
what makes the window observable rather than ambiguous.

**B. Reconciliation: one order, one worker.**

```sql
SELECT … FROM orders WHERE id = $1 FOR UPDATE;
```

Without the row lock, two passes can both read `UNKNOWN`, both query the venue,
and both apply a conclusion — and if they disagree, the last writer wins
silently. `FOR UPDATE` makes it single-flight. This is the concrete reason
[ADR 0002](adr/0002-database-hosting.md) rejected SQLite.

**C. Fill application.** Insert the fill and update the order total in one
transaction. Separately, they can diverge; the phase 8a model validator
(`_fills_agree_with_filled_quantity`) would then reject the record it just
loaded.

---

## 6. Tenant isolation

Flagged in the audit as **absent, not deferred**. It must be designed into
phase 1 rather than retrofitted, because retrofitting isolation means auditing
every query written before it existed.

Required:

1. **Every table carrying user data has an `owner_id`**, directly or through
   `account_id`.
2. **Every repository method takes the authenticated owner** and filters on it.
   Not "should" — the method signature makes it impossible to omit.
3. **Row-level security as defence in depth.** A `WHERE` clause someone forgets
   is a data leak; an RLS policy is a second wall that does not depend on
   remembering.
4. **No identifier from a request selects an account.** Phase 6's property —
   the account is derived from the session, never named by the caller — becomes
   more important, not less, once more than one exists.
5. **A test that a second user cannot read the first user's orders**, written
   before the endpoints are.

---

## 7. Why `NUMERIC` and `timestamptz` specifically

The audit found zero float contamination in the money path, and phase 0's
`to_decimal` rejects `float` outright. That discipline survives the database
only if the column type does:

- `double precision` would silently round every balance on write. A system that
  refuses `Decimal(0.1)` in Python and stores it as a float in PostgreSQL has
  accomplished nothing.
- `timestamp without time zone` loses the offset. Order timing across a restart
  is UTC or it is wrong, and "wrong by an hour" during a DST change is the kind
  of bug that only appears twice a year.

---

## 8. What 8b adds beyond porting 8a

Three gaps the 8a review left open on purpose, each needing a durable home:

| Gap | Requirement |
|---|---|
| **Submission retry state** | `submission_attempts`, `next_retry_at`. Must be durable, or a retry storm survives a restart invisibly. |
| **Lookup by `venue_order_id`** | An index, for a future push/websocket stream keyed by the venue's id rather than ours. |
| **Per-order concurrency** | `SELECT … FOR UPDATE`, not a global lock. A global lock would serialise every order behind the slowest reconciliation. |

---

## 9. Definition of done for the dependency

Phase 8b may begin when all of these hold. Status after phase 1, verified
against the provisioned PostgreSQL 17.6 rather than asserted:

- [x] `DATABASE_URL` provisioned and reachable
- [x] Alembic configured; migrations run clean from empty, and reverse
- [x] `users`, `accounts`, `orders`, `order_fills`, `order_discrepancies` exist
- [x] Money is `NUMERIC(24,8)`, time is `timestamptz`, verified by round-trip tests
- [x] `UNIQUE (account_id, client_order_id)` enforced **by the database**
- [x] `SELECT … FOR UPDATE` available and used for reconciliation
- [x] `owner_id` on every user-facing table; repositories filter on it
- [ ] **Authentication issues a session that resolves to exactly one owner**
- [x] A cross-tenant read test exists and passes
- [x] Connection failure is an operational state, not an exception that escapes

**The one open item is authentication, and it is open on purpose.** The
isolation *mechanism* is built and enforced by the database: row-level
security, forced, on a runtime role without `BYPASSRLS`, keyed to a
transaction-local GUC. What does not exist yet is a login that decides which
owner that GUC should hold. Until it does, the application resolves a single
bootstrap owner server-side, which preserves phase 6's property exactly -- no
request names or selects an account, so there is no identifier with which to
ask for someone else's.

There is a specific consequence worth recording before someone meets it while
writing login: the `users` policy scopes rows to `id = current_setting(...)`,
so the runtime role **cannot look a user up by email**. It can only confirm the
owner it already names. Authentication therefore needs a lookup the runtime
role is deliberately not allowed to make -- a `SECURITY DEFINER` function or a
second, narrowly-scoped policy. Either is a deliberate hole in the wall and
belongs in the change that introduces login, argued on its own terms.

## 10. What phase 1 did not make durable

`orders`, `order_fills` and `order_discrepancies` are durable. **Paper account
state -- the balance, open positions, the trade log -- is still in memory** and
still reports `Durability.IN_MEMORY`, because §2 never specified a table for it
and inventing one would have changed phase 6 execution semantics under the
cover of a database migration.

So crash recovery is a real guarantee for the order lifecycle and is not
claimed for the paper simulation. A restart loses the simulated balance, as it
always did, and the API goes on saying so on every response.

Until every box is ticked, `order.engine` stays `PARTIAL` and crash recovery
stays unclaimed.
