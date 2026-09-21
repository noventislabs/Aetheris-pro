# ADR 0002 — Database hosting

- Status: **accepted** (was open, and blocked phase 1 from phase 0 until now)
- Date: 2026-09-21
- Decided: 2026-09-21, during phase 8 design

## Context

The specification requires PostgreSQL with migrations. The development machine
has neither PostgreSQL nor Docker installed, and winget's app-execution alias
is broken, so the usual one-command install is unavailable.

The question became urgent at phase 8. Its two capabilities are
`order.engine` ("idempotent submission and **crash recovery**") and
`execution.testnet` ("**isolated credentials and order records**"), and both
phrases point at durable state. Crash recovery over in-memory state recovers
nothing: the crash took the state with it. An order that reached a venue and
whose record did not survive the restart is an order that cannot be reconciled,
and an unreconcilable real order is worse than no order at all.

## Options

1. **Native PostgreSQL install** (EDB installer). Closest to production;
   needs administrator rights and ~300 MB, plus a resident service on a machine
   with ~1 GB free RAM.
2. **Hosted PostgreSQL free tier** (e.g. Neon, Supabase). No local resource
   cost and no admin rights needed; requires a signup and network access, and
   the connection string becomes a real secret.
3. **SQLite for development, PostgreSQL for production.** Cheapest locally, but
   the two diverge exactly where this system is demanding — `NUMERIC`
   semantics, `timestamptz`, row locking for order reconciliation. Divergence
   would be discovered late, in the code that handles money.

## Decision

**Option 2 — hosted PostgreSQL free tier.**

The deciding factor is the resource budget rather than a preference for hosted
infrastructure: this machine has roughly 1 GB free, already runs a backend test
suite and a Next.js build, and adding a resident database service to that is
the kind of constraint that surfaces as mysterious flakiness rather than as a
clean failure.

Option 3 stays rejected, and phase 8 sharpens why. The three things SQLite
diverges on — `NUMERIC` semantics, `timestamptz`, and `SELECT … FOR UPDATE` row
locking — are precisely the three things the order engine depends on: exact
money round-trips, UTC order timing across a restart, and single-flight
reconciliation of one order. Divergence would be discovered in the code that
handles real orders, which is the worst place to discover anything.

## Consequences

- **Phase 1 is unblocked.** Authentication, persistence and migrations can
  begin once a connection string exists.
- **Provisioning needs the maintainer.** A signup cannot be automated from
  here. Phase 1 starts when `DATABASE_URL` is available.
- **The connection string is a real secret.** It is already modelled as
  `SecretStr`, already absent from `.env.example` except as a placeholder, and
  already required in production by `_production_requires_secrets`.
- **Network dependency.** The database is now a remote service, so a dropped
  connection is an operational state the persistence layer must handle rather
  than assume away — the same discipline already applied to venue data.
- **Phase 8 sequencing follows from this.** Its pure half (the order state
  machine, idempotent identity, the reconciliation protocol) needs no database
  and can proceed; its durable half and testnet execution wait for phase 1.
  Recorded in [phase-8-design.md](../phase-8-design.md) §1.
