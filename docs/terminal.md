# Market terminal

> Status: Phase 3. **The Markets and Scanner pages described here are analysis
> only** — they display market data and rankings and have no order entry. The
> terminal as a whole gained order entry later, on the `/paper` and `/testnet`
> pages, which are covered in their own documents. **No page stores or handles
> a credential**, and the frontend never signs a venue request.

## 1. Stack, and what was deliberately left out

| Choice | Version | Why |
|---|---|---|
| Next.js App Router | 15.5.25 | Routing and build; static prerender of the shell |
| React | 19.3.0 | |
| TypeScript (strict) | 5.9.3 | `noUncheckedIndexedAccess` on |
| Vitest + Testing Library | 2.1.9 | Lighter than Jest; shares the Vite transform |
| ESLint (flat config) | 9.39.5 | `eslint .`, not the deprecated `next lint` |

**No charting library.** lightweight-charts, Recharts and D3 each add 40–400 kB
plus a canvas or virtual-DOM layer re-instantiated on every symbol change. This
chart draws a few hundred SVG rectangles and two axes; on a machine with ~1 GB
free RAM the cost is real and the benefit is not.

**No CSS framework.** Plain CSS with custom properties. Avoiding an extra build
step and its watcher process matters more here than authoring convenience.

**No state-management or data-fetching library.** One ~90-line hook
(`useApiResource`) covers loading/success/error plus visibility-aware polling.

Result — production build:

```
Route (app)                    Size  First Load JS
/markets                    4.22 kB         109 kB
/scanner                     3.3 kB         108 kB
+ First Load JS shared                      102 kB
```

## 2. Routes

| Route | State |
|---|---|
| `/` | Redirects to `/markets` |
| `/markets` | **Built** — symbol search, timeframes, chart, ticker header |
| `/scanner` | **Built** — bounded table with sort, filter, paging |
| `/backtest` | **Built** — config form, metrics, equity curve, trade list |
| `/paper` | **Built** — order entry, positions, trades, order history |
| Falcon, Watchlist, Account | **Not built** |

Unbuilt sections appear in the navigation as **disabled items tagged
PLANNED**, with a tooltip naming the phase they are scheduled for. They are not
links to empty pages. Showing them keeps the roadmap visible; linking them
would be a promise the build cannot keep.

## 3. Data states

Every market-data component handles six states explicitly. There is no
"fall back to something plausible" path anywhere in the client.

| State | Rendering |
|---|---|
| **Loading** | Skeleton with `role="status"` and a labelled phase |
| **Success** | Backend values, formatted |
| **Stale** | Amber dot, `STALE` pill, **no numbers** — the backend withheld them |
| **Unavailable** | Status code plus the backend's own `detail`, verbatim |
| **Error** | Machine-readable code, message, request ID, Retry |
| **Empty** | Distinguishes "no match" from "feed broken" by reporting the universe size |

An absent value renders as an em dash (`—`), never `0`. A zero is a claim about
the market; a dash is visibly not a number.

### The `OK` + `age_seconds: null` case

Phase 2 established that an `OK` observation with an unverifiable age must not
be read as fresh. The terminal honours this: the freshness badge shows **"age
unverified"** and uses the *unknown* (grey) dot, not the healthy green one.
A test asserts the green dot is absent in that case.

A **negative** age is not an error — a kline series reports the newest bar's
close time, and that bar is normally still forming, so it closes in the future.
That renders as **"forming"**.

## 4. Markets page

- **Symbol search** — queries `/markets/symbols?search=`, so the list is always
  the venue's current universe. No symbol list is bundled.
  Keyboard: `/` focuses from anywhere (unless already typing), `↑`/`↓` move,
  `Enter` selects, `Escape` closes without selecting.
- **Timeframes** — `1m 5m 15m 1h 4h 1d`, exactly the set the backend accepts.
- **Chart** — candles, volume, crosshair, OHLCV tooltip, dashed last-price line
  with axis marker, and time labels. It renders exactly the candles supplied:
  it never interpolates a gap, extends a series or synthesises a bar. A candle
  that fails to parse is dropped, not guessed at.
- **Header** — price, bid, ask, 24h change/high/low, volume, quote volume, plus
  the source, status and age of the ticker.
- **Connection** — the venue connection status from `/markets/status`.

Selected symbol and timeframe persist in `localStorage` (read after mount, so
server and client markup agree). That is a **per-browser convenience only** —
see §7.

### Indicators and SMC

The markets page states plainly, in the panel footer, that indicators (SMA,
EMA, RSI, MACD, Bollinger, VWAP, ADX) and Smart Money Concepts overlays are
**NOT AVAILABLE**, because no backend calculation exists for them yet (phase
4). Nothing is drawn to make the chart look complete.

## 5. Scanner page

Sortable columns for every field the backend ranks, paging, a search filter, a
metric timeframe selector, and an opt-in "candle metrics" toggle (forced on when
sorting by a candle-derived field, since the data is required).

Two notices are always rendered with results:

- **Ranking scope** — when the backend reports `LIQUIDITY_POOL`, the page says
  the ordering covers the N most liquid instruments rather than all 528.
- **Score meaning** — the Market Opportunity Score disclaimer, in full.

Hovering a score shows every component's raw measurement, contribution and
explanation, plus the disclaimer again. When a score is absent the cell says
*why* — "Not enough closed candles", "Candles unavailable", "Not requested" —
rather than a dash that could read as zero.

## 6. Responsive behaviour

Verified layout targets: **1920, 1440, 1366, 820, 390 px**.

- `overflow-x: hidden` on `body`; no element has a `min-width` exceeding the
  viewport.
- Below **720 px** the 10-column table is replaced by a **card list carrying the
  same fields** — not a reduced set. A 10-column table at 390 px either becomes
  unreadable or scrolls the page sideways.
- The chart is an SVG with a `viewBox`, so it scales without reflow.
- The nav scrolls horizontally within its own bar rather than wrapping the
  page; the top bar wraps to multiple rows on narrow screens.
- `prefers-reduced-motion` disables the only animation (the loading shimmer).

## 7. Watchlist and persistence

**No server-side persistence exists** — there is no database until phase 1
completes (ADR 0002). The terminal therefore stores only two values in
`localStorage`: the selected symbol and timeframe.

These are **temporary, non-authoritative, per-browser preferences**. They are
not a watchlist feature, they do not sync, and nothing depends on them — every
access is wrapped in `try/catch` and the defaults work when storage is blocked.
The Watchlist nav item is correspondingly marked `PLANNED`.

**No credential, API key, secret or token is stored in the browser.** There are
none in this build to store.

## 8. API contract safety

`src/lib/api.ts` validates every response at the boundary before it reaches a
component:

- Unexpected shape → `INVALID_RESPONSE`, rendered as an error state. **A
  malformed backend response cannot crash the dashboard.**
- The observation invariant is re-checked client-side: `OK` must carry a value,
  and any other status must not. A contract regression surfaces as an error
  state rather than a half-rendered panel.
- Backend error codes (`EXCHANGE_UNAVAILABLE`, `EXCHANGE_RATE_LIMITED`, …) are
  preserved and displayed with the request ID.
- An unreachable backend is reported distinctly as `NETWORK_UNREACHABLE`.

`NEXT_PUBLIC_API_BASE_URL` is the only configurable URL, and it is build-time
configuration — no user input reaches a fetch target, so the client cannot be
pointed at an arbitrary host.

## 9. Polling

| Resource | Interval |
|---|---|
| Ticker | 12 s |
| Candles | 45 s |
| Scanner | 30 s |
| Connection status | 60 s |

All polling **pauses while the tab is hidden** and refreshes immediately on
return. Polling a background tab spends the venue rate-limit budget and the
laptop battery on numbers nobody is looking at.

## 10. Commands

```bash
cd frontend
pnpm install
pnpm dev         # development
pnpm build       # production build
pnpm start       # serve the build
pnpm lint        # eslint .
pnpm typecheck   # tsc --noEmit
pnpm test        # vitest run
```

The backend must be running at `NEXT_PUBLIC_API_BASE_URL` (default
`http://127.0.0.1:8000`). Its CORS allowlist includes both `http://localhost:3000`
and `http://127.0.0.1:3000` — a browser treats them as different origins, and
allowing only one silently breaks every request from the other. It permits
`GET, POST, OPTIONS`: `POST` arrived with the paper trading routes and nothing
else, so there is still no `PUT`, `PATCH` or `DELETE`.

## 11. The paper trading desk

The only page that writes. Three things are permanent fixtures rather than
dismissible notices, because the moment a user forgets which mode they are in is
the moment the numbers start meaning something they do not mean:

- the `PAPER / SIMULATION ONLY / NO REAL ORDER` badge;
- `PAPER STATE: IN-MEMORY — RESETS ON RESTART`;
- that management runs only while the page is open **when the phase 7
  autonomous loop is disarmed**, which is the default. With it armed the server
  polls on its own and the tab can be closed.

The autonomy panel states the loop's state as a badge rather than a subtle
toggle position, because "is this thing trading right now?" should be
answerable from across a room. Its decision log shows every iteration,
including `NO_SIGNAL` and `SKIPPED` rows — a log of only the interesting entries
cannot distinguish an idle loop from a dead one.

Refusals are rendered as prominently as fills, with their `RISK_REJECTED_*`
code and the full leverage constraint chain. A risk limit that fires silently
teaches a user that limits do not exist. Refused orders stay in the order
history for the same reason.

The header badge reads **NO REAL ORDERS**, not `READ-ONLY`. That changed in
phase 6: paper trading writes, so the older claim stopped being true, and a
badge that overstates the guarantee is worse than one that states the real one
precisely.

Detail in [paper-trading.md](paper-trading.md).

## 12. Not implemented

- Watchlist (server-side), Falcon, Account pages
- SMC overlays
- Depth/orderbook, trades feed, funding, open interest
- Websocket streaming — REST polling only, per the RAM budget
- Real order entry, in any mode — the paper desk sends nothing to any exchange
