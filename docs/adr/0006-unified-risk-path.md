# ADR 0006 — One risk authority for every order path

- Status: **accepted** — approved 2026-09-21 with three decisions (§N)
- Date: 2026-09-21 (impact analysis added same day)
- Raised by: the 2026-09-21 repository audit, §4.2
- Blocks: phase 8c
- Does **not** block: phase 1, phase 8b

---

## B. Context

Phase 7 delivered `engines/risk/` as a mode-independent authority, and the
documentation describes it as ruling on every order. Phase 8a added
`RECONCILIATION_PENDING` to it. The audit then established that the
documentation is ahead of the code.

Current state, verified in source:

- Phase 8a is committed (`a68d257`) and CI-validated via `906253e`.
- Phase 1 / `DATABASE_URL` remains blocked. This ADR does not depend on it.
- No testnet execution exists and none should start.
- `exchange_max_leverage` is `None` everywhere and always; `>1x` is fail-closed.

---

## C. Problem

Two defects, one visible and one latent.

### C.1 `engines/risk/` has exactly one caller

```
manual     → services/paper.py ─────────────────→ phase 6 gate → fill
autonomous → engines/risk/ → services/paper.py → phase 6 gate → fill
```

A human submitting `POST /api/v1/paper/orders` never reaches the phase 7
engine. The phase 6 gate is thorough but is a strictly smaller check set.

### C.2 The approved leverage never binds

Both paths terminate in `services/paper.py`, which re-resolves leverage
independently:

| Call site | `risk_engine_available` | `risk_max_leverage` |
|---|---|---|
| `services/paper.py:315` — manual **and** the loop's final submit | `False` | `None` |
| `engines/risk/engine.py:295` — loop pre-check only | `True` | derived |

The risk engine's approved **size** binds (the loop passes
`margin=verdict.approved_margin`). Its approved **leverage** is discarded and
recomputed by a resolver told the risk engine does not exist.

Today this is conservative: with `risk_engine_available=False` the resolver can
only return 1x or a refusal, so nothing exceeds 1x by any route. It becomes
dangerous the moment `exchange_max_leverage` is knowable — which is precisely
what phase 8c's credentials do.

---

## D. Options considered

| | Approach | Verdict |
|---|---|---|
| **A** | Risk engine above both callers; verdict threaded to the engine and used | **Recommended** |
| **B** | Keep call sites; make `submit_order` require a `RiskVerdict`; delete `_resolve_leverage` | Pushes work onto every caller — how this happened |
| **C** | Accept two paths; correct the documentation | Zero risk today; **rejected for 8c** — two divergent paths to a real venue is the shape of an incident |

Explicitly **not** an option: forcing everything to 1x. That satisfies the
letter of safety while deleting the mechanism that produces it, and would be
indistinguishable from a broken chain.

---

## E. Option A — detailed design

`PaperTradingService.submit_order` calls `engines/risk/evaluate` once, for every
caller, before the phase 6 gate. The autonomous loop stops calling it directly
and simply submits.

**Four changes are required**, and two of them are not obvious:

1. **Build a `RiskAccountView` in the service.** Derivable from the existing
   account snapshot. Straightforward.
2. **Build a `RiskMarketView` — which needs ATR.** ⚠️ **The manual path does not
   fetch candles.** It fetches a ticker and cached exchange info, nothing else.
   The risk engine *hard-refuses* when `atr_percent is None`. So Option A adds a
   `/klines` call to every manual order. See §F.3.
3. **Track `last_entry_at` per symbol in the service.** ⚠️ Currently tracked only
   in the autonomous loop's memory (`services/paper.py` contains zero
   references). The cooldown cannot work on the manual path without it.
4. **Thread the verdict down and stop re-resolving.** `submit_order` takes the
   `RiskVerdict`; `engines/paper/engine.py` uses `verdict.leverage` instead of a
   second `resolve_leverage` call. `_resolve_leverage` in `services/paper.py` is
   deleted, not deprecated.

The phase 6 gate **stays** and still re-checks independently. Two gates remains
the design; neither trusts the other.

---

## F. Exact behavioural changes

### F.1 Which checks become active for manual orders

Refusal codes each gate can emit, verified by source enumeration:

| Code | Phase 6 gate (today) | Risk engine (Option A) |
|---|---|---|
| MODE_NOT_ENABLED · EMERGENCY_STOP · DAILY_PROFIT_TARGET · DAILY_LOSS_LIMIT · STALE_DATA · SYMBOL_LIMIT · MAX_OPEN_POSITIONS · MAX_LEVERAGE · POSITION_SIZE · INSUFFICIENT_BALANCE · MIN_NOTIONAL · EXCHANGE_PRECISION · PORTFOLIO_EXPOSURE | ✅ | ✅ |
| **COOLDOWN** | ❌ | ✅ **new** |
| **ABNORMAL_VOLATILITY** | ❌ | ✅ **new** |
| **RECONCILIATION_PENDING** | ❌ | ✅ **new** |

**The delta is exactly three codes.** Answering the question directly:

- **COOLDOWN** — newly active. 300s default.
- **ABNORMAL_VOLATILITY** — newly active. 15% measured ATR ceiling.
- **daily loss controls** — already active. No change.
- **emergency stop** — already active. No change.
- **leverage checks** — already active, and **the outcome does not change**. §F.4.
- **position / exposure checks** — already active. No change.
- **sizing checks** — already active; one code becomes more accurate. §F.2.
- **other** — RECONCILIATION_PENDING becomes active, but is inert for paper
  (paper has no venue orders, so the count is always 0 until 8c).

### F.2 Every scenario whose outcome changes

Both gates run over identical inputs. **Five of fourteen scenarios change:**

| Scenario | Today | Option A | Kind |
|---|---|---|---|
| Re-entry 60s after the last | ACCEPTED | **COOLDOWN** | intentional safety |
| Symbol at 40% measured ATR | ACCEPTED | **ABNORMAL_VOLATILITY** | intentional safety |
| **ATR unmeasurable (new listing)** | ACCEPTED | **STALE_DATA** | ⚠️ compatibility — §F.3 |
| An order is unreconciled | ACCEPTED | **RECONCILIATION_PENDING** | intentional safety (inert until 8c) |
| Margin above balance | POSITION_SIZE | **INSUFFICIENT_BALANCE** | code accuracy, not accept/reject |

Unchanged: ordinary orders, re-entry after 400s, no stop supplied, 2x/3x/500x,
daily loss limit, emergency stop, notional below venue minimum.

### F.3 The one change that is not purely a safety improvement

**A manual order on an instrument whose ATR cannot be measured would be
refused.** That happens for a newly listed perpetual with fewer than 15 closed
candles, and whenever `/klines` is unavailable.

This is a **compatibility change with a real cost**, not a free win:

- Every manual order gains a `/klines` fetch. ATR(14) needs ≥15 closed candles;
  ~60 is comfortable. **Do not reuse the autonomous default of 300** — that is
  five times the data for no benefit.
- A new failure mode: an order refused because candle data was unavailable,
  where today only the ticker mattered.
- Latency: one extra round trip, mitigated by the existing 10s kline cache.

**Judgement:** correct but not free. Sizing a position without knowing its
volatility is precisely what `ABNORMAL_VOLATILITY` exists to prevent, and
`None` must mean "refuse", not "assume calm" — but the cost is real and the UI
must explain the refusal rather than showing a bare STALE_DATA on a symbol whose
*price* is perfectly fresh.

### F.4 Leverage — no observable change

Verified across the full range under Option A with `exchange_max_leverage=None`:

| Requested | With stop | Without stop |
|---|---|---|
| **1x** | **ACCEPTED** | **ACCEPTED** |
| 1.5x · 2x · 3x · 125x · 500x | MAX_LEVERAGE | MAX_LEVERAGE |

Identical to today. `risk_max_leverage` becomes *derived and reported* instead
of `None`, but `exchange_max_leverage` is still unknown, so the chain still
fails closed above the domain minimum. Omitting a stop yields `risk_max = None`,
which changes nothing because the chain already cannot complete.

**Option A does not unlock leverage. It makes the chain honest.**

---

## Leverage invariant (as required)

After Option A the flow is exactly:

```
requested leverage
      ↓
exchange max        (None today; real in 8c)
      ↓
risk max            (derived: stop distance ÷ liquidation buffer, daily budget)
      ↓
approved leverage   (min of all three, only when all known)
      ↓
final paper order   ← uses the approved verdict, does not recompute
```

**There is no second resolver.** `services/paper.py::_resolve_leverage` is
deleted. A test asserts `risk_engine_available=False` appears nowhere in
`services/` or `engines/paper/`, so the path cannot be reintroduced quietly.

## Safety invariant (as required)

Every order — manual, autonomous, future testnet, future live — passes
`engines/risk/evaluate` exactly once, and the submitted order never exceeds the
verdict in leverage, size or notional. Asserted at the submission boundary and
by an architecture test that `engines/risk/evaluate` has no bypass parameter.

---

## G. API / UI impact

**API contracts: no breaking change.**

- `SubmitOrderBody` is unchanged. `stop_loss_percent` stays optional — omitting
  it still yields 1x.
- `PaperOrderResult` is unchanged in shape. Three new values can appear in the
  existing nullable `rejection_code` field.
- **Recommended addition** (non-breaking): surface `checks_performed` so a
  refusal is self-explanatory.

**UI: no code change required.** `src/app/paper/page.tsx` renders
`order.rejection_code` generically at three sites; the frontend hardcodes no
code list. New codes render correctly today.

**UI change recommended, not required:** a bare `RISK_REJECTED_STALE_DATA` on a
symbol whose price is visibly fresh is confusing. The refusal detail already
says "volatility could not be measured" — the panel should show the detail, not
only the code.

---

## H. Migration / compatibility impact

| Surface | Impact |
|---|---|
| API shape | none |
| Frontend code | none required |
| Existing tests | ~4 in `test_paper_api.py` assert a manual order is accepted; they need an ATR-capable fixture (`/klines` stub), not a weakened assertion |
| Documentation | README "risk engine has final authority" **becomes true**; `paper-trading.md` §3 and `autonomous-trading.md` §2 need the single-path description |
| Persisted data | none — nothing persists |
| Config | no new settings; `entry_cooldown_seconds` and `max_atr_percent` already exist |

**Sequencing: after 8b, before 8c.** Not before 8b, because 8b already touches
the submission path for durable records and doing both at once makes the diff
unreviewable. Not after 8c, because 8c is when divergence stops being
theoretical.

---

## I. Security impact

**Strictly positive, with one operational caveat.**

- Closes the divergence where the path that would reach a venue is the one that
  never consulted the risk engine.
- Makes "no order bypasses the risk engine" a property of the code rather than
  of the caller, which is what `TradingPort`-style structural guarantees
  elsewhere in this system rely on.
- Removes a second leverage resolver — one fewer place for a ceiling to be
  computed differently.
- No new credential, endpoint, or external surface.
- **Caveat:** one extra `/klines` call per manual order consumes venue
  rate-limit budget. Bounded by the existing cache and backoff; worth stating.

---

## J. Test requirements

Before Option A can be called complete:

1. Manual and autonomous orders produce **identical verdicts** for identical
   inputs — the property the whole ADR exists to establish.
2. The five changed scenarios in §F.2, each asserting its new code.
3. `risk_engine_available=False` appears nowhere in `services/` or
   `engines/paper/` (source-level).
4. `_resolve_leverage` no longer exists in `services/paper.py`.
5. The submitted order never exceeds the verdict — leverage, quantity, notional
   — on both paths.
6. Leverage outcomes unchanged: 1x accepted, everything above refused, with and
   without a stop.
7. An ATR-unmeasurable symbol is refused with a detail naming volatility, not a
   bare STALE_DATA.
8. Cooldown fires within 300s and not after.
9. The phase 6 gate still runs and can still refuse after the risk engine
   approves — two gates, independently.
10. The existing 1104 backend and 133 frontend tests still pass.

---

## K. Rollback considerations

**Cheap to roll back, and worth noting why.**

- No schema, no persisted state, no API shape change — reverting is a code
  revert, nothing to migrate.
- The phase 6 gate is untouched and remains a complete gate on its own, so a
  revert returns to today's behaviour rather than to an ungated path.
- Risk of *partial* rollback: reverting the service while keeping the loop's
  change would leave orders with no risk check at all. The change must revert
  as one commit, and the test in §J.3 fails loudly if `_resolve_leverage`
  reappears alone.
- The three new refusals are configurable (`entry_cooldown_seconds`,
  `max_atr_percent`), so an over-tight threshold is a config fix, not a revert.

---

## L. Recommendation

**Adopt Option A**, sequenced after 8b and before 8c, with two amendments the
analysis produced:

1. **Fetch ~60 candles for the manual path, not 300.** ATR(14) needs 15.
2. **Show the refusal detail in the UI, not only the code** — so an
   ATR-unmeasurable refusal reads as what it is.

The cost is honest: one extra venue call per manual order, and orders that
succeed today can begin to fail. Both are the intended consequence of putting
manual orders under the same authority as autonomous ones.

---

## M. The decision to approve

> Adopt **Option A**: `engines/risk/evaluate` becomes the single authority for
> every paper order path, manual and autonomous; the approved verdict binds the
> submitted order; `services/paper.py::_resolve_leverage` is deleted; the phase
> 6 gate remains as an independent second check.
>
> Accepting that:
> - manual orders gain **COOLDOWN**, **ABNORMAL_VOLATILITY** and
>   **RECONCILIATION_PENDING**;
> - manual orders on instruments with unmeasurable ATR become **refused**;
> - every manual order costs one additional `/klines` fetch;
> - leverage behaviour is **unchanged** — 1x only, until a venue ceiling exists;
> - implementation happens **after phase 8b and before phase 8c**, never before
>   `DATABASE_URL` resolves phase 1.

---

## N. Decisions taken (2026-09-21)

The three open questions were answered on approval.

### N.1 — Newly listed symbols: **REFUSE**

Fewer than 15 usable candles for ATR(14) refuses the order. The risk engine must
not approve when a required volatility measurement cannot be computed from
sufficient real market data. **ATR is never fabricated and never substituted.**

This needs a distinct machine-readable code, because "there is not enough
history" is a different condition from "the price is stale" — in the former the
price may be perfectly fresh. Introduced:

```
RISK_REJECTED_INSUFFICIENT_HISTORY
```

and a `VolatilityStatus` carried on the market view, so the eight cases the UI
must distinguish each arrive with their own code and detail rather than four of
them collapsing into `STALE_DATA`.

### N.2 — `checks_performed`: **YES**

The manual order response exposes which risk checks ran, in order. Backward
compatible: a new optional field on `PaperOrderResult`, nothing removed or
renamed. It names checks only — no thresholds, no account internals, no secrets.

### N.3 — Manual cooldown: **60s, separately configured**

A human is not a loop on 15-minute bars. `AETHERIS_RISK_MANUAL_ENTRY_COOLDOWN_SECONDS`
defaults to **60**; the autonomous loop keeps `entry_cooldown_seconds` at **300**,
unchanged. `RiskPolicy` is already constructed per caller, so this needs no new
mechanism — each path supplies its own cooldown and the engine is indifferent.

**Autonomous cooldown behaviour is unchanged and must stay unchanged without
separate approval.**

---

## O. Found during implementation

Two defects the change itself created, both caught by existing tests.

### O.1 — Idempotency was bypassed by the new authority

Putting the risk engine above the engine meant a **replayed** `client_order_id`
reached it and was re-ruled. The first attempt opened a position; the retry was
refused for `COOLDOWN` — a *different answer to the one already applied*, which
is the exact failure idempotency exists to prevent.

Fixed: the service consults `PaperEngine.replay` **before** evaluating risk. A
retry returns what happened the first time, never a fresh verdict.

### O.2 — `COOLDOWN` masked `SYMBOL_LIMIT`

`last_entry_at` is stamped when a position opens, so holding one always implies
a recent entry. With the cooldown checked before the position slots, the
reported reason for "you already hold this" was "wait 60s" for the entire
cooldown window — less actionable, and wrong about the cause. The cooldown's own
detail says it exists to stop re-entry churn after an *exit*; if the position is
still open, nobody is re-entering.

Fixed: position slots now precede pacing. Order of authority is

```
mode → emergency → reconciliation → daily lock → data quality
     → symbol / position slots → cooldown → volatility → leverage → sizing
```

### O.3 — Accepted cost: the autonomous path fetches candles twice

The loop fetches ~300 bars for its strategy; the service then fetches 60 for the
volatility it measures itself. Deliberate. The alternative — letting the caller
supply an ATR — reopens a trust boundary immediately after closing one: a caller
that can supply a measurement can supply a false one. The risk authority
measures its own inputs.
