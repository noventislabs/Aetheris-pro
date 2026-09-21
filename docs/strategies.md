# Strategy analysis

> Status: Phase 4. **Analysis only.** A strategy here produces a reading of
> indicator values, never an instruction. Nothing in this system places an
> order, sets leverage, or holds a credential, in any mode.

## 1. What a result is, and is not

`LONG_BIAS` means *four named conditions are satisfied right now*. It says
nothing about what price will do next.

There is deliberately **no confidence, probability, win rate or expected return
anywhere in this layer.** What a reader gets instead is every condition the rule
set evaluated, the actual number it measured, and whether it held — which is
checkable, whereas a confidence number is not. A test asserts those field names
are absent from the result model.

`NEUTRAL` is a finding in its own right (conditions conflicted). It is distinct
from the analysis *not having run*, which the status carries instead. When the
status is not `READY`, `bias` is `null` — reporting NEUTRAL would imply an
analysis was made.

## 2. The baseline strategy

**Trend-Momentum Confluence**, version `1.0.0`. Four indicators, each answering
a different question, so agreement between them is informative and disagreement
is an honest NEUTRAL:

| Input | Question it answers |
|---|---|
| EMA fast vs slow | Which direction is the trend? |
| **ADX** | Is there a trend worth reading at all? |
| RSI | Is momentum behind it, and not already exhausted? |
| MACD histogram | Is momentum still accelerating, independent of the EMA periods? |

```
LONG_BIAS   when  EMA(fast) > EMA(slow)
                  AND ADX >= adx_minimum
                  AND 50 < RSI < rsi_overbought
                  AND MACD histogram > 0

SHORT_BIAS  when  EMA(fast) < EMA(slow)
                  AND ADX >= adx_minimum
                  AND rsi_oversold < RSI < 50
                  AND MACD histogram < 0

NEUTRAL     otherwise
```

**All four must agree.** Three of four is NEUTRAL. A rule set that fires on
partial agreement is a rule set with an undocumented tie-break, and this one has
none.

**ADX gates rather than votes.** It measures trend strength without direction,
so a weak reading blocks *both* directions. Without it the EMA cross alone
produces a constant stream of flip-flopping bias in a ranging market.

Both directions are always evaluated and both condition sets returned, so a
NEUTRAL shows how close each side came.

### Defaults

| Parameter | Default | Validation |
|---|---|---|
| `ema_fast` / `ema_slow` | 21 / 55 | fast **must** be < slow |
| `rsi_period` | 14 | 2–200 |
| `rsi_overbought` / `rsi_oversold` | 70 / 30 | (50,100) / (0,50) |
| `adx_period` / `adx_minimum` | 14 / 20 | 2–200 / 0–100 |
| `macd_fast` / `slow` / `signal` | 12 / 26 / 9 | fast **must** be < slow |

An inverted fast/slow pair is **rejected, not reordered**: silently swapping
them would answer a different question from the one asked.

Warm-up is the maximum of the individual warm-ups — the strategy is ready only
when its slowest input is. At the defaults that is 33 bars.

## 3. The freshness gate

A strategy must never return `READY` on data it cannot vouch for.

| Condition | Result |
|---|---|
| Candles `STALE` or unavailable | `STALE`, no bias |
| **`OK` but `age_seconds` is null** | `STALE`, no bias |
| Candle order ambiguous or duplicated | `UNAVAILABLE`, no bias |
| Too few bars | `INSUFFICIENT_DATA`, no bias |

The second row is the Phase 2 rule carried through: an `OK` observation with no
verifiable age means the venue supplied no event timestamp. That is not evidence
of freshness, and anything trading-oriented must refuse it.

## 4. Two entry points

```python
evaluate_from_candles(candles, symbol=..., timeframe=...)   # pure
evaluate(key, candles, context, now, params)                # + freshness gate
```

The pure form is what the **phase 5 backtester** calls: historical bars have no
meaningful "age", and gating on one would make every backtested bar unusable.
The live form wraps it in the gate above.

## 5. Leverage architecture

> **1x–500x is the candidate range — the range analysis may *ask* for. It is not
> a range this system may use, and 500x is not a guaranteed usable leverage.**

Four deliberately separate fields:

| Field | Meaning | Today |
|---|---|---|
| `requested_leverage` | What analysis asked for, 1–500 | derived from ATR |
| `exchange_max_leverage` | The venue's real per-symbol ceiling | **null — unknown** |
| `risk_max_leverage` | The risk engine's ceiling | **null — no engine yet** |
| `approved_leverage` | What may actually be used | **null above 1x — see below** |

```
approved = min(requested, exchange_max, risk_max)   -- only if all are known
```

### Fail closed

If **any** constraint is unknown, the decision is a rejection with a named
reason, not a fallback to the request. An unknown ceiling is not permission.

The exchange maximum is never guessed and never assumed to be 500. Venues serve
per-symbol leverage brackets only from authenticated endpoints, the ceilings
differ by symbol and by notional tier, and most perpetuals cap far below 500x.
`Symbol.max_leverage` is therefore `null` for public data.

Constraints are also **validated, not just checked for presence**:

| Supplied value | Result |
|---|---|
| `None` | `*_UNKNOWN` rejection |
| Not a `Decimal` (int, float, str) | `*_INVALID` rejection — a float already lost precision |
| `< 1x` (zero, negative, fractional) | `*_INVALID` rejection |
| `> 500x` | `*_INVALID` rejection — **refused, not clamped**, because no rule declares how to reduce it |

### The one exception: exactly 1x

A request for exactly `LEVERAGE_MIN` resolves without the ceilings, and the
reason is arithmetic rather than judgement. Every ceiling this domain accepts is
at least 1x, so `min(1, any valid ceiling)` is 1 whatever the missing values turn
out to be — learning them could not change the answer. And 1x is unlevered:
margin equals notional, nothing is borrowed, and the liquidation this chain
exists to prevent has no mechanism.

It applies at exactly 1x, only when the chain could not otherwise complete, and
never when a supplied ceiling is malformed. It carries its own reason code,
`LEVERAGE_APPROVED_AT_DOMAIN_MINIMUM`, so it is never mistaken for an approval
the full chain produced. Full reasoning in
[ADR 0004](adr/0004-leverage-at-the-domain-minimum.md).

This is what lets [paper trading](paper-trading.md) open a position at all. A
paper request above 1x is refused exactly as it is here.

### How the candidate is derived

```
candidate = (2% reference adverse move / ATR%) × condition agreement,
            clamped to 1–500x
```

**ATR volatility + deterministic strategy-condition agreement → leverage
candidate.** A more volatile instrument moves further against a position for the
same event, so it warrants less leverage.

`agreement` is a count divided by a count — the fraction of the strategy's
conditions that hold. It is **not an AI confidence, a probability of profit, a
win probability, an AI certainty, or an AI-selected execution leverage**, and it
authorises nothing. It can only ever scale a candidate **down**: 4/4 leaves it
untouched, 1/4 quarters it. Condition counts are validated (`total > 0`,
`0 ≤ met ≤ total`) so the ratio can never exceed 1.

No candidate is produced at all when volatility is unmeasurable, the counts are
incoherent, or nothing agrees (0/N) — rather than defaulting to 1x, which would
read as a deliberate conservative choice rather than an absence.

### The authoritative chain

```
market data → indicators → strategy analysis → requested leverage
            → exchange constraint → RISK ENGINE → approved leverage → execution
```

The Risk Engine has final authority and will weigh volatility, stop distance,
liquidation distance, account equity, position size, the daily loss limit,
symbol constraints and exchange limits. It is phase 7 work.

**Phase 4 adds no order placement and no leverage-setting call.** There is no
such endpoint, no `set_leverage`, and no implementation of `TradingPort`; a test
asserts no leverage or order path string appears anywhere in the package.

Live example (BTCUSDT 1h, real data):

```
REQUESTED     4x      (2% / ATR 0.4169% × 4/4 agreement)
EXCHANGE MAX  unknown
RISK MAX      unknown
APPROVED      none
REJECTED — RISK_REJECTED_EXCHANGE_MAX_LEVERAGE_UNKNOWN
```

## 6. Separation from the scanner

The **Market Opportunity Score** (phase 3) and **strategy analysis** (phase 4)
are different analytical layers and are deliberately not merged:

| | Opportunity Score | Strategy analysis |
|---|---|---|
| Question | Which instruments are unusually active? | Do these indicators agree on a direction? |
| Output | A 0–100 ranking number | A bias plus its conditions |
| Use | Ordering a table | Reading one instrument |

A high score does not imply a bias, and a bias does not imply a high score.

## 7. API

`GET /api/v1/analysis/strategies` — the catalogue, with the rule set in plain
language and the disclaimer.

`GET /api/v1/analysis/{symbol}/strategy` — evaluate. Parameters are typed and
bounded; an unknown strategy key is a 422 listing what is registered.

Every result carries the disclaimer as a field, so it survives being copied into
a screenshot, a log line or a downstream payload.
