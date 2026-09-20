# ADR 0002 — Database hosting (open)

- Status: **open — blocks phase 1**
- Date: 2026-09-21

## Context

The specification requires PostgreSQL with migrations. The development machine
has neither PostgreSQL nor Docker installed, and winget's app-execution alias
is broken, so the usual one-command install is unavailable.

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

Deferred pending the maintainer's choice. Option 3 is not recommended for this
system's accounting requirements.

## Consequences

Phase 1 (authentication, persistence, migrations) cannot begin until this is
resolved. Phase 0 deliberately persists nothing, and readiness reports the
database as `NOT_CONFIGURED` rather than pretending otherwise.
