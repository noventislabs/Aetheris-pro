# ADR 0001 — Layered architecture with a pure core

- Status: accepted
- Date: 2026-09-21

## Context

A trading system accumulates engines fast: strategy, risk, backtest, paper,
order, portfolio. Without a dependency rule they grow into each other, and the
first casualty is testability — you end up needing a live exchange connection
to test an indicator.

## Decision

Dependencies point downward through five layers: delivery → orchestration →
risk authority → integration → foundation. `core/` and `domain/` are pure: no
I/O, no network, no framework imports.

The risk engine sits in its own layer between orchestration and integration, so
the only code path to execution passes through it.

## Consequences

- Indicators, risk rules and backtests are unit-testable with no network.
- The backtest engine and the live path can share the same strategy code,
  because strategies depend only on pure layers.
- Cost: some data must be mapped across layer boundaries rather than passed
  through as an exchange payload. Accepted deliberately — the mapping is where
  provenance and decimal conversion get enforced.
