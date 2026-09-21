# Indicator engine

> Status: Phase 4. Pure arithmetic over candles — no I/O, no framework, no
> venue, no settings. The phase 5 backtester calls the same function the
> terminal does.

## 1. Numeric type

`Decimal` throughout, matching the rest of the system. The alternative — float
for indicators, Decimal for money — was rejected deliberately: the boundary
between "an indicator value" and "a number that reaches a position size" is
exactly where a silent precision loss would hide, and this engine is designed
to feed the risk engine later. The cost is bounded: a 1500-candle series is a
few thousand Decimal operations.

Where a value is mathematically undefined, functions return `None`. They never
substitute a neutral-looking default such as 50 or 0 — an invented midpoint is
indistinguishable downstream from a measured one.

## 2. Conventions

Each indicator follows one named, published definition. Where several accepted
definitions exist the choice is explicit, because an RSI computed with an EMA
instead of Wilder's smoothing is not a variant — it is a different number that
disagrees with every reference chart.

| Indicator | Kind | Lines | Convention | Warm-up |
|---|---|---|---|---|
| **SMA** | overlay | `sma` | Unweighted mean of close | `period − 1` |
| **EMA** | overlay | `ema` | `k = 2/(period+1)`, seeded with the SMA of the first `period` closes (TA-Lib convention) | `period − 1` |
| **Bollinger** | overlay | `upper` `middle` `lower` | Middle = SMA(close); bands at ±`deviations` × **population** stdev (divisor N) | `period − 1` |
| **VWAP** | overlay | `vwap` | **Rolling window**, not session-anchored | `period − 1` |
| **RSI** | oscillator | `rsi` | **Wilder's smoothing (RMA)**, not an EMA | `period` |
| **MACD** | oscillator | `macd` `signal` `histogram` | EMA(fast) − EMA(slow); signal = EMA(signal) of that line; both SMA-seeded | `slow + signal − 2` |
| **Stochastic** | oscillator | `k` `d` | **Slow** form: %K = SMA(smooth_k) of raw %K; %D = SMA(period_d) of %K | `period + smooth_k + period_d − 3` |
| **ATR** | oscillator | `atr` | **Wilder's smoothing** of true range | `period` |
| **ADX** | oscillator | `adx` `plus_di` `minus_di` | Wilder throughout: smoothed ±DM/TR → DI → DX → Wilder average of DX | `2 × period − 1` |
| **ROC** | oscillator | `roc` | `100 × (close_i − close_{i−period}) / close_{i−period}` | `period` |
| **CCI** | oscillator | `cci` | Lambert's constant 0.015; **mean absolute deviation**, not standard deviation | `period − 1` |

### Two conventions worth calling out

**VWAP is rolling, not session-anchored.** Textbook VWAP resets at the session
open, but a perpetual future trades continuously and has no session boundary.
Anchoring to an arbitrary UTC midnight would make the value depend on when the
chart happened to be loaded.

**Wilder's smoothing is not an EMA of the same period.** It is equivalent to an
EMA of period `2n − 1`. RSI, ATR and ADX are all defined with it, and a test
asserts the two diverge so nobody "simplifies" one into the other.

**One ATR across the product.** The phase 3 scanner originally used a plain mean
of true ranges. It now routes through this engine's Wilder ATR, so the number in
the scanner is the number on the chart. The opportunity score was bumped to
`market-opportunity/v2` for that reason.

## 3. Warm-up and status

| Status | Meaning |
|---|---|
| `READY` | Every line has a value at the latest bar |
| `WARMING_UP` | The series is long enough, but the latest bar has no value — a slower line has not caught up, or the value is undefined at that bar |
| `INSUFFICIENT_DATA` | The supplied series is shorter than the parameters need; nothing can be computed |
| `UNAVAILABLE` | Candles could not be retrieved, or arrived stale |

**No value is ever emitted early.** A test asserts, for every registered
indicator, that the declared warm-up is the real one: at exactly `warmup` bars
the status is not READY, and at `warmup + 1` it is. A declared warm-up that is
too short makes a caller trust a value that does not exist; too long makes it
discard a valid one.

## 4. No look-ahead

Every value at bar *i* uses bars ≤ *i*. True range uses the *previous* close,
never the next one.

This is enforced by a regression test that computes each indicator on the full
series and on truncated prefixes and asserts they agree at every shared index,
plus a second test asserting that appending a bar never rewrites history. It
runs for all eleven indicators.

This matters beyond correctness: the same engine feeds the phase 5 backtester,
and an indicator that peeks at a future bar makes every backtest silently
optimistic — a failure that produces no error, no warning and a beautiful
equity curve.

## 5. Candle preparation

Indicator maths assumes closed, ascending, unique bars. Rather than assume that,
every calculation goes through `prepare_candles` first:

| Input | Result |
|---|---|
| Ascending | used as-is |
| Descending (newest-first) | **reversed** — unambiguous to correct |
| Duplicate timestamps | **refused** — which bar is authoritative is unknowable |
| Neither ascending nor descending | **refused** — the intended order cannot be inferred |
| Trailing bars not yet closed | **dropped**, and counted |
| Nothing left | reported, not returned empty silently |

Forming bars are dropped because a partial bar has partial volume and an
incomplete high/low; letting one in makes the newest value wrong on every
refresh.

## 6. API

### `GET /api/v1/analysis/indicators`
The catalogue: every implemented indicator with its kind, lines, parameters,
defaults and **convention**. The terminal reads this rather than hardcoding a
list, so an indicator cannot appear in the UI before its implementation exists.

### `GET /api/v1/analysis/{symbol}/indicators`

| Parameter | Default | Bounds |
|---|---|---|
| `indicators` | `ema,rsi,macd` | comma-separated; whitelist; **max 8** |
| `timeframe` | `1h` | the six supported frames |
| `limit` | 300 | 20–1000 candles |
| `series_points` | 0 | 0–500 trailing points |
| `<name>_period` etc. | see catalogue | per-parameter bounds |

`GET /api/v1/analysis/{symbol}/indicators/{indicator}` is the single-indicator
form.

**Injection is impossible by construction.** The indicator list is a closed
whitelist of registered callables; parameters are typed integer/decimal fields
with declared ranges. There is no expression, formula string, callable name,
`eval`, `exec` or `compile` anywhere in the package — a test asserts that.

An unknown key is a 422 listing what *is* supported, rather than a silent
omission that would hand back a response quietly lacking a line.

## 7. Testing

- **40 hand-calculated values.** Every expected number is derived from the
  published formula by hand, with the arithmetic written out in the test. None
  were produced by running the implementation and pasting the output — a test
  written that way only asserts the code still does whatever it did first.
- **No-look-ahead**, 11 indicators × 4 cut points, plus append-safety.
- **Edge cases**: flat markets, unbroken advances and declines, zero volume,
  zero-range windows, gaps through the previous close, micro-cap prices at
  1e-8, volumes near 1e11, duplicate and shuffled timestamps.
