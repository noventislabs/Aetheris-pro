# Backtesting engine

> Status: Phase 5. A backtest reports what a rule set **would have done** over
> bars that already closed, under the assumptions below. It is not a
> prediction, not an expected return, and not evidence the rules will work
> again. Every result ships that disclaimer as a field.

## 1. Where it sits

Pure, like the layers it builds on. `run_backtest(candles, …)` imports no HTTP
client, no venue adapter, no framework and no settings — the architecture test
enforces it — so a simulation runs over a fixture file as readily as over live
history. Only `services/backtest.py` touches the network.

It reuses Phase 4 exactly as that phase was designed to be reused: the same
indicator functions and the same `evaluate_conditions` rule set the live
terminal shows. There is no separate "backtest version" of the strategy that
could drift from the one being analysed.

## 2. How a bar is processed

Order is the whole correctness story:

1. **Execute** the action decided on the *previous* bar, filling at this bar's
   open.
2. **Manage** the open position against this bar's high/low.
3. **Mark** equity to this bar's close.
4. **Decide** from indicator values at this bar, scheduling for the next.

Deciding *last* is what prevents look-ahead: a signal computed from bar *i* can
only ever act on bar *i+1*.

### One-pass indicators

Indicator series are computed once over the whole history rather than
recomputed on every prefix — O(n) instead of O(n²). That is only valid because
no indicator reads forward, which Phase 4 asserts. A dedicated test recomputes
the bias on truncated prefixes and requires bar-for-bar agreement, so the
optimisation cannot silently drift from the semantics it claims.

## 3. The assumptions, stated

These are returned on every result and served from `/backtest/method`, because
a profit factor without its fill model is not interpretable.

**Signals fill at the next bar's open**, not at the close that produced them.
Filling at the same close assumes zero latency between a bar closing, an
indicator recomputing and an order resting — which is how a backtest quietly
becomes optimistic.

**When a bar could hit both the stop and the target, the stop wins.** OHLC
cannot say which came first inside a bar. Assuming the favourable one is the
single most common way a backtest flatters itself, so this engine always
assumes the adverse path. Among several adverse levels (stop, trailing stop,
liquidation) the one nearest entry fires, since it is reached first.

**Trailing stops trail on closed bars.** The level checked on bar *i* derives
from extremes up to bar *i−1*, then updates with bar *i*'s extreme afterwards.
Updating first and then checking the same bar would assume an intrabar ordering
the data does not contain.

**Liquidation is modelled simply and optimistically.** A position closes when
its loss reaches the posted margin. Real venues liquidate *earlier*, at a
maintenance-margin threshold that varies by symbol and notional tier, and they
charge a fee. Leveraged results here are therefore **better than reality** and
must not be read as a liquidation study.

**Fees** are charged per side on notional; **slippage** moves every fill
adversely — buys fill higher, sells fill lower.

### Not modelled

Funding payments · partial fills · order-book depth · maker rebates and fee
tiers · borrow costs · exchange downtime · per-symbol lot-size and
minimum-notional filters · maintenance-margin liquidation tiers.

Each is listed on the result rather than left for a reader to discover.

## 4. Configuration

| Parameter | Default | Bounds |
|---|---|---|
| `starting_balance` | 100 | > 0, ≤ 10,000,000 |
| `position_size_percent` | 10 | > 0, ≤ 100 — margin posted per position |
| `leverage` | 1 | 1–**25** |
| `fee_bps` | 5 | 0–100 per side |
| `slippage_bps` | 2 | 0–100 |
| `stop_loss_percent` | 2 | > 0, ≤ 90, or off |
| `take_profit_percent` | 4 | > 0, ≤ 1000, or off |
| `trailing_stop_percent` | off | > 0, ≤ 90 |
| `allow_long` / `allow_short` | both on | at least one required |
| `limit` | 500 | 60–1500 candles |

**Leverage is capped at 25x, not 500x.** The 1–500x range is the *candidate*
domain for what live analysis may request; a simulation offering 500x would be
a study of liquidation rather than of a strategy, and would invite reading the
output as a plan. Simulation leverage is an input that authorises nothing and
sets nothing — the live leverage chain is separate and still approves nothing.

## 5. Metrics

Trades, win/loss/breakeven counts, win rate, net PnL, gross profit and loss,
total fees, return %, profit factor, average/largest win and loss, max drawdown
(% and absolute), Sharpe-like ratio, exposure %, bars tested, trades open at
end.

**An undefined statistic is `null`, not a sentinel.** A profit factor with no
losing trades is not "infinity"; a Sharpe-like ratio over a flat curve is not
zero. Substituting a value would put a number on a chart that no computation
produced.

**"Sharpe-like", not "Sharpe".** It is the annualised mean over standard
deviation of per-bar equity returns with a zero risk-free rate — a
dispersion-adjusted comparison number, not the textbook ratio. Naming it
precisely is cheaper than a footnote nobody reads.

**Drawdown is measured on the full curve**, before the curve is thinned to 500
points for transport. The trough of a drawdown is exactly what a sampler is
most likely to drop.

## 6. Warnings

Returned with the result and rendered beside the numbers, never hidden:

- Fewer than 20 trades — "the win rate and profit factor describe a handful of
  coincidences rather than a strategy"
- A position still open when data ended, closed at the last bar and marked
  `END_OF_DATA` — that trade did not exit on a rule
- Leverage above 1x — a simulation input, with an optimistic liquidation model
- No trades at all — the metrics describe an untouched balance

## 7. API

`GET /api/v1/backtest/method` — the fill model and the list of gaps in it.

`GET /api/v1/backtest/{symbol}` — run a simulation. `GET`, like every route in
this build: a backtest computes and returns, persisting nothing and placing
nothing, so it is a read even though it does real work. Keeping it a GET
preserves the architecture test's flat assertion that no non-GET route exists.

## 8. Verified against live data

BTCUSDT 1h, 999 real candles (10 Aug – 21 Sep 2026):

```
trades  41 (19W/22L)   win rate 46.3%   profit factor 0.79
net     -0.2476 USDT   return -0.24%    fees 0.41
maxDD   0.73%          exposure 19.9%   Sharpe-like -1.42
```

The baseline strategy **loses** over that window. That is the engine working:
a backtester whose first real result is a profit is usually a backtester with a
bug.

Config changes behave as expected on the same data — a tight stop/target
produces `STOP_LOSS` and `TAKE_PROFIT` exits, a trailing stop produces 21
`TRAILING_STOP` exits, and 10x leverage amplifies the loss to −1.39%.

## 9. Not included in this phase

**Hyperparameter optimisation** is registered as `PLANNED`. Building it on top
of a backtester is straightforward mechanically and dangerous analytically —
without train/test separation and overfitting warnings it produces
impressive-looking parameters that describe the past and nothing else. It gets
its own phase rather than being bolted on here.
