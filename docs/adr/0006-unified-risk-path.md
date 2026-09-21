# ADR 0006 — One risk path for every order (DRAFT — proposal, not a decision)

- Status: **proposed — awaiting review. Nothing is implemented.**
- Date: 2026-09-21
- Raised by: the 2026-09-21 repository audit, §4.2
- Blocks: phase 8c

> This ADR deliberately stops short of deciding. It states the problem, the
> invariant, and three options with a recommendation. **The behaviour changes
> only once this is approved.**

## Context

The audit found that the two ways an order can be created do not pass through
the same checks.

**1. `engines/risk/` has exactly one caller.** A manual
`POST /api/v1/paper/orders` never reaches it:

```
manual     → services/paper.py → engines/paper/risk.py (phase 6 gate) → fill
autonomous → engines/risk/ (phase 7) → services/paper.py → phase 6 gate → fill
```

So a human submitting an order is not subjected to the cooldown check, the
`ABNORMAL_VOLATILITY` check, or the derived leverage ceiling. The phase 6 gate
is thorough, but it is a different and smaller set of checks.

**2. The risk engine's approved leverage never binds a fill.** Both paths end
in `services/paper.py`, which re-resolves leverage independently:

| Call site | `risk_engine_available` | `risk_max_leverage` |
|---|---|---|
| `services/paper.py:315` — manual **and** the loop's final submit | `False` | `None` |
| `engines/risk/engine.py:295` — loop pre-check only | `True` | derived |

The risk engine's approved **size** does bind — the loop passes
`margin=verdict.approved_margin`. Its approved **leverage** is discarded and
recomputed by a resolver that was told the risk engine does not exist.

**Today this is conservative, not dangerous.** With `risk_engine_available=False`
the paper path can only ever return 1x (the domain minimum) or a refusal, so
nothing can exceed 1x by any route. The problem is latent: the moment
`exchange_max_leverage` becomes knowable — which is exactly what phase 8c's
credentials do — the two paths compute different answers and the one that
reaches the venue is the one that never consulted the risk engine.

The README's claim that fills happen "behind a risk engine that has final
authority" is therefore not currently accurate.

## The invariant this should establish

```
requested leverage → exchange ceiling → risk ceiling → approved leverage → final order
```

with two properties, both testable:

1. **Every order, whatever created it, passes through `engines/risk/` exactly
   once**, and the verdict it produces is the one that reaches the venue.
2. **The submitted order never exceeds the approved verdict** — not in leverage,
   not in size, not in notional. A single assertion at the submission boundary,
   comparing what is about to be sent against what was approved.

Explicitly **not** the invariant: "everything is 1x". Forcing the floor would
satisfy the letter of safety while removing the mechanism that makes it safe,
and would be indistinguishable from the chain being broken.

## Options

### A. Move the risk engine above both paths *(recommended)*

`PaperTradingService.submit_order` calls `engines/risk/evaluate` first, for
every caller. The loop stops calling it directly and simply submits; the
service does it once. The verdict is threaded through to the engine and the
approved leverage is **used**, not recomputed.

- One path, one verdict, one place to audit.
- The phase 6 gate stays and still re-checks independently — two gates remains
  the design, and neither trusts the other.
- Cost: `services/paper.py` must build a `RiskAccountView`, which the loop
  currently does. Moderate change to a phase 6 file.

### B. Pass the verdict down and forbid re-resolution

Leave the call sites, but make `submit_order` require a `RiskVerdict` and
delete `_resolve_leverage` entirely. The manual endpoint would have to obtain a
verdict before submitting.

- Smallest change to the loop.
- Pushes the work onto every caller, which is how the inconsistency happened.

### C. Leave it, and correct the documentation

Accept two paths; change the README to say the phase 6 gate is the authority
for manual orders.

- Zero risk today.
- **Rejected for 8c.** Two divergent paths to a real venue is the shape of an
  incident, and the divergence appears exactly when credentials arrive.

## Recommendation

**Option A**, before phase 8c and after phase 8b.

Not before 8b, because 8b already touches the submission path to add durable
records, and doing both at once makes the diff unreviewable. Not after 8c,
because 8c is the phase where the divergence stops being theoretical.

## Consequences if accepted

- `engines/risk/` becomes the single authority in fact as well as in
  documentation, and the README claim becomes true.
- Manual paper orders gain the cooldown, volatility and derived-ceiling checks.
  **This is a behaviour change**: orders that succeed today may be refused.
  That is the point, and it should be tested for explicitly rather than
  discovered.
- The autonomous loop gets simpler — it proposes and submits; the service rules.
- The paper path's structural 1x ceiling disappears, replaced by the real chain.
  Since `exchange_max_leverage` remains `None` until 8c, the *observable*
  behaviour stays 1x-only until credentials exist. That is the chain working,
  not a coincidence.
- A new test asserts the submitted order never exceeds the approved verdict, on
  both paths.

## Open questions for review

1. **Is a behaviour change to manual paper orders acceptable?** Option A means
   a manual order can now be refused for `COOLDOWN` or `ABNORMAL_VOLATILITY`.
2. **Should the manual endpoint expose the verdict?** Returning the checks
   performed would make a refusal self-explanatory, at the cost of a larger
   response.
3. **Does phase 6's gate stay?** The recommendation keeps it. Removing it would
   leave one gate, and the two-gate property has caught real ordering defects.
