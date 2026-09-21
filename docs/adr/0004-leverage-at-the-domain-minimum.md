# ADR 0004 — Approving leverage at the domain minimum

- Status: accepted
- Date: 2026-09-21
- Amends: the fail-closed leverage chain of phase 4

## Context

The leverage chain resolves `approved = min(requested, exchange_max, risk_max)`
and refuses when any constraint is unknown. Today both are unknown: venues serve
per-symbol leverage brackets only from an authenticated endpoint and this build
holds no credentials, and the risk engine is phase 7. So the chain approves
nothing at all.

Phase 6 needs to open paper positions. With nothing approvable, the paper engine
could never fill a single order, and the phase would ship a desk that refuses
everything.

Two ways out were rejected outright:

- **Supply a simulation leverage ceiling**, as the backtester does. The
  backtester's leverage is an explicit input to a study of past bars; a paper
  position is a live simulation running the real constraint chain. Feeding it an
  invented ceiling would make the chain report an approval that no constraint
  justified.
- **Assume a venue maximum.** Explicitly forbidden, and the single most
  important thing the chain exists to prevent.

## Decision

A request for **exactly `LEVERAGE_MIN` (1x)** resolves without the ceilings.
Everything above it still fails closed, unchanged.

The justification is arithmetic, not judgement. `_validate_ceiling` defines a
usable ceiling as one in `[LEVERAGE_MIN, LEVERAGE_MAX]`. For any ceiling this
domain would accept, `min(1, ceiling) == 1`. Learning the missing values could
not change the answer, so refusing would not be failing closed — it would be
refusing a value already proven safe against every constraint the chain can
express.

It is also the boundary between borrowing and not borrowing. At 1x margin equals
notional: nothing is lent, and the leverage-driven liquidation the chain exists
to prevent has no mechanism.

Three guards keep it narrow:

- It applies at **exactly** 1x. 1.0001x still needs both ceilings.
- It applies only when the chain **could not complete**. A fully-known chain
  still reports `APPROVED_IN_FULL` at 1x, as it always did.
- It **declines** when a ceiling was supplied and is malformed, letting the
  chain report the real defect rather than routing around it.

It carries its own reason code, `LEVERAGE_APPROVED_AT_DOMAIN_MINIMUM`, so it is
never mistaken for an approval the full chain produced.

## Consequences

- Paper trading works, at 1x, with no constraint invented anywhere.
- A user requesting 10x on the paper desk sees the fail-closed architecture
  work against a real request: `exchange_max_leverage: null`,
  `approved_leverage: null`, reason
  `RISK_REJECTED_EXCHANGE_MAX_LEVERAGE_UNKNOWN`. The refusal is exercised
  against real traffic long before it guards real money.
- The safety model is not weakened. Nothing above 1x became approvable, the
  configured `max_leverage` ceiling is untouched, and no venue constraint is
  assumed.
- Five tests pin the rule and its three guards, including one asserting the
  safety property directly: at the approved leverage, margin equals notional.
- Cost: a reader skimming `resolve_leverage` now sees an approval path that
  bypasses the ceilings, which looks like a loophole until the derivation is
  read. The function that implements it is named and documented for exactly
  that reason.
