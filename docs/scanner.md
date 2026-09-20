# Market scanner

> Status: Phase 3. **Read-only market intelligence.** The scanner analyses and
> ranks. It places no orders, holds no credentials, and depends on a port that
> has no execution methods.

## 1. The cost problem, and how the design answers it

The eligible universe is **528 USDT-M perpetuals** (live count). A naive
scanner fetches a ticker and a candle series per instrument per request —
roughly a thousand upstream calls to render one page of a table. That is not a
slow endpoint; it is a rate-limit ban and an out-of-memory error on an 8 GB
machine.

So work is split by what each field actually costs:

| Field group | Upstream cost | Coverage |
|---|---|---|
| Price, 24h change, volume, bid/ask | **One** whole-market request | Entire universe |
| Volatility, ATR, momentum, trend, opportunity score | One request **per instrument** | Bounded pool only |

That produces two modes, and **the response always says which one ran**:

`FULL_UNIVERSE`
: Ordering by a ticker field. Every eligible instrument was ranked, from a
  single bulk snapshot.

`LIQUIDITY_POOL`
: Ordering by — or filtering on — a candle-derived field. Metrics were computed
  for the most liquid N instruments and the ranking covers that pool.
  `candidate_pool_size` reports how many. **A subset is never presented as the
  whole market.**

### Bounds

| Setting | Default | Ceiling |
|---|---|---|
| `AETHERIS_SCANNER_DEFAULT_PAGE_SIZE` | 25 | — |
| `AETHERIS_SCANNER_MAX_PAGE_SIZE` | 100 | 200 |
| `AETHERIS_SCANNER_CANDIDATE_POOL_SIZE` | 60 | 120 |
| `AETHERIS_SCANNER_MAX_METRIC_SYMBOLS` | 60 | 120 |
| `AETHERIS_SCANNER_METRIC_CONCURRENCY` | 6 | 16 |
| `AETHERIS_SCANNER_CANDLE_LIMIT` | 60 | 500 |
| `page` (API) | 1 | 200 |

`max_metric_symbols` is a hard ceiling on candle requests for any single scan,
independent of page size. Rows beyond it keep `metrics_status:
NOT_REQUESTED` — they are labelled, not dropped.

## 2. The universe is discovered, never hardcoded

The scanner consumes the Phase 2 eligibility predicate: `PERPETUAL`,
`USDT`-quoted, `TRADING`, positive tick and step. It contains no symbol names.
A newly listed perpetual enters the scan the moment the venue lists it — live
verification returned `ZECUSDT`, `AKEUSDT` and `FFUSDT` in the top ranks purely
by discovery.

## 3. Candle statistics

Computed by `aetheris.analysis.metrics`, a pure module — no I/O, no venue
knowledge, deterministic for a given input and clock.

| Metric | Definition |
|---|---|
| `window_return_percent` | Close-to-close change across the window |
| `momentum_percent` | Close-to-close change over the last 10 bars |
| `volatility_percent` | Population standard deviation of per-bar percent returns |
| `atr` / `atr_percent` | Mean true range, absolute and as a share of last close |
| `relative_volume` | Last closed bar's volume ÷ mean of the preceding bars |
| `range_percent` | Window high-to-low, over the low |
| `body_percent` | Mean body ÷ mean bar range |
| `trend` | `UP` / `DOWN` / `SIDEWAYS`, threshold ±0.25% net move |
| `trend_consistency` | Fraction of bars closing with the net direction (0–1) |

Two correctness properties:

**Closed bars only.** The newest bar a venue returns is normally still forming,
so its volume is a partial count and its range is incomplete. Including it
would make relative volume read low and ranges read narrow for *every*
instrument on *every* scan. Forming bars are dropped before any maths runs.

**No look-ahead.** Every value at bar *i* uses bar *i* and earlier. True range
uses the previous close, never the next one.

**Minimum 15 closed bars.** Below that the result is
`INSUFFICIENT_DATA` — not an estimate, not zeros. A just-listed instrument with
three bars of history gets no statistics.

## 4. Market Opportunity Score

> **The Market Opportunity Score is a deterministic market-analysis and ranking
> metric. It is NOT a probability of profit, an expected return, a win rate, a
> trading signal, a confidence value, or a prediction of future price.**

It is a weighted sum of four present-tense measurements, used to order a table.
A score of 80 does not mean an 80% chance of anything. It means the instrument
scores highly against four published measurements relative to published
reference values.

```
score = 100 × ( 0.30 × relative_volume_component
              + 0.25 × volatility_component
              + 0.25 × momentum_component
              + 0.20 × trend_consistency_component )
```

Each component is clamped to 0–1 against a reference value, so the weights
summing to 1 bound the score to 0–100 by construction:

| Component | Measurement | Reference (= fully expressed) | Weight |
|---|---|---|---|
| `relative_volume` | last bar volume ÷ preceding mean | 3× | 0.30 |
| `volatility` | ATR as % of last close | 3% | 0.25 |
| `momentum` | \|% move over lookback\| | 5% | 0.25 |
| `trend_consistency` | fraction of bars agreeing with net direction | 1.0 | 0.20 |

The reference values are the judgement calls in the formula. They are explicit,
versioned (`market-opportunity/v1`) and published at
`GET /api/v1/scanner/scoring-method`, so they can be argued with rather than
reverse-engineered.

**Momentum counts as magnitude, not direction** — a sharp fall is as notable as
a sharp rise. Direction is reported separately as `trend`, so it is never lost,
only kept out of a magnitude measure.

**A score is withheld entirely** when `relative_volume` is unavailable. That
component carries the largest weight, and a score missing it is not comparable
with the others; an incomparable number in a sorted column is worse than an
absent one.

Every score ships with its components — raw measurement, normalized value,
weight and contribution — so any number the UI shows traces back to arithmetic.
Live example (`FFUSDT`, score 81.32):

```
relative_volume    raw=16.3543  norm=1.0000  w=0.30 -> 30.00
volatility         raw=1.9376   norm=0.6459  w=0.25 -> 16.15
momentum           raw=19.3649  norm=1.0000  w=0.25 -> 25.00
trend_consistency  raw=0.5085   norm=0.5085  w=0.20 -> 10.17
                                                      ------
                                                       81.32
```

## 5. Freshness is judged per instrument

This corrected a real defect found against live Binance. Snapshot freshness was
originally judged on the **oldest** ticker in the bulk response — but in a
528-contract snapshot a thinly traded perpetual is routinely minutes behind, so
one quiet instrument marked the entire market stale and blanked all 528 prices.

The rule now:

- **Per row** — each instrument's `ticker_status` is judged against its *own*
  last-trade timestamp. A quiet contract is `STALE` and carries no prices,
  while an actively traded one in the same response is `OK`. Neither borrows
  the other's credibility.
- **Per snapshot** — the page-level `ticker_status` is judged on the *newest*
  ticker, which answers a different question: "is this feed alive at all?" If
  even the most recently traded instrument is old, the feed has stopped, and
  that is a genuine outage.

A `STALE` row carries `last_price: null` and a `ticker_detail` explaining why.
The Phase 0 invariant holds throughout: **unavailable is never zero.**

## 6. Sorting

| Field | Scope |
|---|---|
| `symbol`, `last_price`, `price_change_percent_24h`, `volume_24h`, `quote_volume_24h` | `FULL_UNIVERSE` |
| `volatility_percent`, `momentum_percent`, `atr_percent`, `relative_volume`, `trend_consistency`, `opportunity_score` | `LIQUIDITY_POOL` |

There is deliberately **no sort by confidence, prediction or signal strength**,
because no such quantity exists in this system.

**Unavailable values sort last in both directions** and are ordered among
themselves by symbol. They are never coerced to zero — doing so would rank an
instrument with no data above one with a genuine negative value, which is a
fabricated comparison. Symbol is the tie-break everywhere, so equal values
order identically on every call and pagination stays stable between pages.

A row missing the value a filter tests is **excluded**: "volume at least X"
cannot be satisfied by an unknown volume.

## 7. API

### `GET /api/v1/scanner`

| Parameter | Type | Default | Bounds |
|---|---|---|---|
| `search` | string | — | ≤ 32 chars; matches symbol or base asset |
| `sort` | enum | `quote_volume_24h` | see table above |
| `direction` | `asc` \| `desc` | `desc` | |
| `page` | int | 1 | 1–200 |
| `page_size` | int | 25 | 1–100 |
| `timeframe` | enum | `1h` | `1m`/`5m`/`15m`/`1h`/`4h`/`1d` |
| `include_metrics` | bool | `false` | costs one request per returned row |
| `quote_asset` | string | — | ≤ 16, `^[A-Za-z0-9]+$` |
| `min_quote_volume` | decimal | — | ≥ 0 |
| `min_price_change_percent` | decimal | — | −100 … 10000 |
| `max_price_change_percent` | decimal | — | −100 … 10000 |
| `min_volatility_percent` | decimal | — | 0 … 1000 — forces `LIQUIDITY_POOL` |
| `min_relative_volume` | decimal | — | 0 … 1000 — forces `LIQUIDITY_POOL` |
| `trend` | enum | — | forces `LIQUIDITY_POOL` |

Anything out of bounds is a `422` with the standard error envelope, before any
upstream work happens.

### `GET /api/v1/scanner/scoring-method`

Publishes the formula: method version, weights, minimum candles, momentum
lookback, sideways threshold, and the disclaimer above. A ranking number a user
cannot interrogate invites them to read meaning into it that is not there.

## 8. Errors

Upstream faults surface with the Phase 2 vocabulary, never as an empty table —
zero rows would read as "no instruments match", a different and misleading
claim.

| Condition | Status | Code |
|---|---|---|
| Venue unreachable / 5xx | 503 | `EXCHANGE_UNAVAILABLE` |
| Rate limited (incl. HTTP 418) | 429 | `EXCHANGE_RATE_LIMITED` |
| Malformed upstream payload | 502 | `EXCHANGE_INVALID_RESPONSE` |
| Invalid parameter | 422 | `VALIDATION_FAILED` |

A per-instrument candle failure degrades **only that row** to
`metrics_status: UNAVAILABLE` with a reason. One delisted-mid-scan instrument
does not take the table down.

## 9. What the scanner does not do

- **No indicators** — SMA, EMA, RSI, MACD, Stochastic, Bollinger, VWAP, ADX,
  ROC, CCI are phase 4 and report `PLANNED`. The scanner's statistics are a
  separate, narrower set.
- **No Smart Money Concepts** — HH/HL/LH/LL, BOS, CHoCH, FVG, liquidity zones,
  order blocks, premium/discount are phase 4 and report `PLANNED`. Nothing is
  drawn to make the UI look complete.
- **No AI, no confidence, no predictions** — there is no model here.
- **No orders, in any mode.**
- No websocket streaming; REST only, per the machine's RAM budget.
