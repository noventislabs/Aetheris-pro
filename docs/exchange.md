# Exchange layer

> Status: Phase 2. **Read-only.** This layer connects to public market data and
> nothing else. It holds no credentials and has no code path to an order.

## 1. Shape of the layer

```
api/v1/markets.py          HTTP surface, normalized schemas
        │
services/market_data.py    orchestration, search ranking
        │
adapters/exchange/ports.py MarketDataPort  (read-only contract)
        │                  TradingPort     (declared type, implemented by nothing)
        │
adapters/exchange/binance/ adapter · parsing · endpoints
        │
adapters/exchange/http.py  retry, timeout, error mapping
adapters/exchange/cache.py bounded TTL cache
        │
                           Binance USDT-M Futures public REST
```

Nothing above `adapters/` knows Binance exists. `parsing.py` is the only module
that names Binance's fields and array layouts; everything it returns is a
normalized `aetheris.domain.market` model. A second venue means a second
adapter, not a change to the service or the API.

### The two ports

`MarketDataPort` is an ABC the Binance adapter implements. `TradingPort` is a
`Protocol` — a type with **no implementation anywhere in the codebase**. It
exists so the eventual execution shape is visible and so the architecture test
has something concrete to assert against. There is no runtime path to an order:
`create_order` and friends are types, not code.

## 2. Configuration

All values are operator configuration. No exchange credentials exist in this
phase, for any mode.

| Environment variable | Default | Purpose |
|---|---|---|
| `AETHERIS_BINANCE_FUTURES_REST_BASE_URL` | `https://fapi.binance.com` | Public REST base |
| `AETHERIS_BINANCE_REQUEST_TIMEOUT_SECONDS` | `10.0` | Per-request read timeout |
| `AETHERIS_BINANCE_CONNECT_TIMEOUT_SECONDS` | `5.0` | Connection timeout |
| `AETHERIS_BINANCE_MAX_RETRIES` | `3` | Retries after the first attempt (max 5) |
| `AETHERIS_BINANCE_BACKOFF_SECONDS` | `0.5` | Base for exponential backoff |
| `AETHERIS_BINANCE_MAX_BACKOFF_SECONDS` | `8.0` | Backoff ceiling |
| `AETHERIS_BINANCE_EXCHANGE_INFO_TTL_SECONDS` | `300.0` | Symbol universe cache |
| `AETHERIS_BINANCE_TICKER_TTL_SECONDS` | `3.0` | Ticker cache |
| `AETHERIS_BINANCE_KLINES_TTL_SECONDS` | `10.0` | Candle cache |
| `AETHERIS_BINANCE_KLINES_CACHE_MAX_ENTRIES` | `16` | Hard entry ceiling |
| `AETHERIS_BINANCE_TICKER_CACHE_MAX_ENTRIES` | `64` | Hard entry ceiling |
| `AETHERIS_BINANCE_MAX_KLINES_LIMIT` | `1500` | Venue ceiling |
| `AETHERIS_MARKET_DATA_MAX_TICKER_AGE_SECONDS` | `30.0` | Older ⇒ `STALE` |
| `AETHERIS_MARKET_DATA_MAX_CANDLE_AGE_MULTIPLE` | `2.5` | Intervals before `STALE` |

The base URL is validated at startup: it must be http/https, carry a host, and
carry no query or fragment. It is the only externally-controlled URL in the
system and **no API parameter can redirect a request elsewhere** — symbols are
validated against a character pattern and then checked against the discovered
universe before any call is made.

## 3. Endpoints consumed

All public, all `GET`, none requiring an API key:

| Path | Used for |
|---|---|
| `/fapi/v1/exchangeInfo` | Venue metadata, the full instrument list, filters |
| `/fapi/v1/ticker/24hr` | 24-hour rolling statistics |
| `/fapi/v1/ticker/bookTicker` | Best bid/ask |
| `/fapi/v1/klines` | OHLCV candles |

`/fapi/v1/ticker/24hr` does not carry bid/ask, so a ticker request fetches both
it and `bookTicker` concurrently. Top-of-book is treated as optional
enrichment: if that one call fails, the ticker is still returned with
`bid_price`/`ask_price` as `null` and the failure is logged. The 24-hour
statistics are not optional — without them there is nothing truthful to return,
so that failure propagates.

## 4. Symbol discovery

Eligibility is a pure predicate over normalized metadata
(`parsing.is_eligible`), containing **no symbol names**:

```
contract_type == PERPETUAL
quote_asset   == USDT
status        == TRADING
tick_size      > 0
step_size      > 0
```

A newly listed perpetual qualifies the moment the venue lists it; a delisted
one drops out on its own. Nothing needs changing in code.

Against live Binance this currently resolves **905 instruments listed, 528
eligible**.

A symbol entry that cannot be parsed is skipped with a logged reason rather
than failing the whole universe — one malformed instrument should not blind the
scanner to the other five hundred. If *every* symbol fails, that is a contract
change rather than a bad row, and the response is rejected outright.

An unrecognised venue status maps to `SymbolStatus.UNKNOWN`, which is not
tradable. A lifecycle state Binance adds tomorrow therefore excludes a symbol
rather than being silently treated as `TRADING`.

### Search

Ranked: exact symbol → exact base asset → prefix → substring. So `BTC` surfaces
`BTCUSDT` above `1000BTTCUSDT`, and `0G` finds `0GUSDT`. Ranking is a product
decision, so it lives in the service layer rather than in the adapter.

## 5. Supported intervals

`1m`, `5m`, `15m`, `1h`, `4h`, `1d` — the `Timeframe` enum, whose values match
Binance's interval strings exactly. An unlisted interval is a 422 before any
network call.

`limit` is bounded to 1–1500 at the API and clamped again in the adapter, so an
absurd request is refused here rather than sent upstream to be rejected.

## 6. Provenance and freshness

Every externally sourced value travels inside `Observation[T]` carrying
`source`, `event_ts`, `received_ts`, `status` and `age_seconds`. The Phase 0
invariant is preserved exactly: **`OK` requires a value; any other status
forbids one.**

A stale ticker therefore returns `status: "STALE"`, `value: null` and a
`detail` explaining why. It does not return the last known price wearing a
stale label.

Staleness rules:

- **Ticker** — older than `max_ticker_age_seconds` (default 30 s).
- **Candles** — the most recent bar's close time older than
  `timeframe_seconds × max_candle_age_multiple` (default 2.5 intervals). The
  newest bar is normally still forming, so its close time lies in the future
  and the series is as fresh as it can be; a venue clock slightly ahead of ours
  yields a small negative age, which is treated as fresh rather than as a fault.
- **Empty series** — a legitimate answer for a just-listed symbol, returned as
  `OK` with no candles.

**`age_seconds` is negative for a forming candle**, and deliberately so. A
kline series reports the newest bar's close time as its `event_ts`, and that
bar is normally still open, so its close lies in the future — a live 1h series
typically reports something like `-3383`, meaning "this bar closes in 56
minutes". It is not clamped to zero, because zero would assert the bar had just
closed, which is untrue. Clients reading it as "how stale is this" should use
`max(age_seconds, 0)`.

**Binance's `exchangeInfo.serverTime` can lag materially** — observed roughly
1.5 days behind `/fapi/v1/time` on a live call, presumably from edge caching of
that large document. It is surfaced verbatim as `server_time` because it is
what the venue said, but **no freshness decision uses it**. Ticker and candle
staleness are judged from `closeTime` values on the data itself, which were
verified against a local clock in agreement with `/fapi/v1/time` to within one
second.

### Caching cannot launder staleness

The caches store **domain objects, never `Observation` envelopes**. Freshness is
recomputed from the exchange timestamp on every serve. A ticker cached while
fresh and read back an hour later is re-judged and returned `STALE` — which a
test asserts directly.

Expiry is a hard miss with eviction. There is no serve-stale-while-revalidate
mode, because a cached price presented as current is exactly the fabrication
this system forbids.

Caches are bounded with LRU eviction (16 kline entries, 64 ticker entries, 1
exchange-info entry) because the target machine has roughly 1 GB of free RAM
and a kline entry can hold 1500 candles. Redis was deliberately not introduced:
it would cost more memory than it saves for a handful of public responses.

## 7. Rate limiting and errors

Faults are separated by what the caller should *do* about them:

| Code | HTTP | Meaning | Retried? |
|---|---|---|---|
| `EXCHANGE_RATE_LIMITED` | 429 | Venue asked us to slow down | yes, honouring `Retry-After` |
| `EXCHANGE_UNAVAILABLE` | 503 | Unreachable, or 5xx | yes |
| `EXCHANGE_TIMEOUT` | 504 | Request or total budget exceeded | yes |
| `EXCHANGE_INVALID_RESPONSE` | 502 | Malformed JSON, missing fields, bad candle | **no** |

Binance's **HTTP 418** (IP auto-ban after repeated 429s) is treated as rate
limiting, not as a generic error — it must be backed off from, not hammered.

Retry policy:

- **Only `GET` is ever retried.** Phase 2 is read-only, but the guard is
  written now so the execution phases inherit it rather than having to remember
  it — retrying a non-idempotent call is how one intended order becomes two.
- **Only transient faults are retried.** A non-429 4xx means our request was
  wrong; repeating it cannot help.
- **A malformed response is never retried.** Asking again for nonsense returns
  nonsense.
- **Attempts and total time are both bounded** — a fixed retry count plus an
  overall deadline, so the worst case is knowable rather than emergent.
- **Backoff has full jitter.** Without it, concurrent symbol requests that hit
  one 429 would all retry at the same instant and trip the limit together.

## 8. Connection status

`GET /api/v1/markets/status` reports observed outcomes only:

| Status | Meaning |
|---|---|
| `UNKNOWN` | No request has been made yet |
| `CONNECTED` | A real response succeeded, and nothing has failed since |
| `DEGRADED` | Worked before, failing now |
| `UNAVAILABLE` | Has never succeeded, and has failed |

Having a base URL configured is **not** evidence of a connection. The endpoint
also does not trigger an upstream call — a monitoring poll must not become
rate-limit pressure — so it reports from cached state.

## 9. Observability

Structured events: `exchange_request`, `exchange_response`,
`exchange_rate_limited`, `exchange_timeout`, `exchange_error`,
`symbol_universe_updated`, `exchange_symbol_skipped`,
`exchange_symbols_partial`.

No credential, key, secret or authentication header is ever logged — there are
none in this phase to log.

## 10. Live smoke test

CI must never depend on a third party being reachable, so live verification is
a manually invoked script rather than a test:

```bash
cd backend
.venv/Scripts/python.exe -m aetheris.tools.binance_smoke
```

It reads public endpoints only, samples whatever symbol the venue listed first
(no hardcoded ticker), and exits non-zero printing `BINANCE_UNAVAILABLE` if the
venue cannot be reached. It never fabricates a result.

## 11. What this layer does not do

- No websockets. REST only, deliberately, given the machine's RAM budget.
- No authenticated endpoints, so **no leverage brackets** — `Symbol.max_leverage`
  is `null` rather than guessed, because only an authenticated endpoint serves
  it.
- No funding rate, open interest, mark price or liquidation data yet.
- No order submission, account or position access, in any mode.
