"use client";

import { useCallback, useState } from "react";
import { ErrorState, LoadingState } from "@/components/DataState";
import { SymbolSearch } from "@/components/SymbolSearch";
import {
  ApiError,
  cancelTestnetOrder,
  getTestnetStatus,
  submitTestnetOrder,
  type TestnetOrderParams,
  type TestnetOrderView,
} from "@/lib/api";
import { formatNumber, formatPrice, NO_VALUE } from "@/lib/format";
import type { TestnetStatus, VenuePositionView } from "@/lib/types";
import { useApiResource } from "@/lib/useApiResource";

/**
 * Binance Demo (testnet) execution desk.
 *
 * The distinction this page exists to protect is not live-versus-simulated --
 * it is *which* simulation. Paper trading is an Aetheris account with a
 * 100 USDT starting balance and no venue behind it. This is a real Binance
 * account on Binance's demo infrastructure: the orders are genuine within that
 * environment, they rest on a real book, and the balance shown is the venue's,
 * not ours. Mixing the two would make both numbers meaningless, so nothing on
 * this page reads or displays paper state.
 *
 * Every value here comes from the backend, which is the only thing holding
 * credentials. Where the venue did not supply a value the page says so rather
 * than printing a zero: an unavailable mark price rendered as 0.00 is a number
 * nobody observed, sitting on a screen that looks authoritative.
 */

/** Slow on purpose. Venue state changes when the user acts, not every second. */
const STATUS_POLL_MS = 10_000;

interface OrderDraft {
  symbol: string;
  side: "BUY" | "SELL";
  margin: string;
  requestedLeverage: string;
  stopLossPercent: string;
  takeProfitPercent: string;
}

const DEFAULT_ORDER: OrderDraft = {
  symbol: "BTCUSDT",
  side: "BUY",
  margin: "20",
  requestedLeverage: "1",
  stopLossPercent: "2",
  takeProfitPercent: "4",
};

function Field({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="tn-field">
      <span className="tn-field-label">{label}</span>
      <span className={tone ? `tn-field-value ${tone}` : "tn-field-value"}>{value}</span>
    </div>
  );
}

function ConnectionBadge({ status }: { status: TestnetStatus }) {
  const map: Record<string, { text: string; tone: string }> = {
    CONNECTED: { text: "Connected", tone: "tn-ok" },
    UNAVAILABLE: { text: "Unavailable", tone: "tn-bad" },
    DISABLED: { text: "Disabled", tone: "tn-off" },
  };
  const shown = map[status.connection] ?? { text: status.connection, tone: "tn-off" };
  return <span className={`tn-badge ${shown.tone}`}>{shown.text}</span>;
}

function PositionRow({ position }: { position: VenuePositionView }) {
  const size = Number(position.quantity);
  const isShort = size < 0;
  const pnl = position.unrealized_pnl;
  return (
    <tr>
      <td>{position.symbol}</td>
      <td className={isShort ? "down" : "up"}>{isShort ? "SHORT" : "LONG"}</td>
      <td className="num">{formatNumber(Math.abs(size).toString())}</td>
      <td className="num">{position.entry_price ? formatPrice(position.entry_price) : NO_VALUE}</td>
      <td className="num">{position.mark_price ? formatPrice(position.mark_price) : NO_VALUE}</td>
      <td className={pnl ? (Number(pnl) < 0 ? "num down" : "num up") : "num"}>
        {pnl ? formatNumber(pnl) : NO_VALUE}
      </td>
      <td>{position.margin_mode ?? NO_VALUE}</td>
      <td className="num">{position.leverage ? `${position.leverage}x` : NO_VALUE}</td>
    </tr>
  );
}

export default function TestnetPage() {
  const [symbol, setSymbol] = useState(DEFAULT_ORDER.symbol);
  const [order, setOrder] = useState(DEFAULT_ORDER);
  const [outcome, setOutcome] = useState<TestnetOrderView | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [orderNonce, setOrderNonce] = useState(0);
  const [actionError, setActionError] = useState<string | null>(null);

  const fetcher = useCallback(
    (signal: AbortSignal) => getTestnetStatus(symbol, signal),
    [symbol],
  );
  const { state, refresh } = useApiResource<TestnetStatus>(fetcher, [symbol], {
    pollMs: STATUS_POLL_MS,
  });

  const status = state.kind === "success" ? state.data : null;
  const account = status?.account ?? null;

  // The intent key is what the backend derives an order identity from, so it
  // decides what counts as "the same order". Holding it steady across retries
  // is the point: a second click, or a retry after a network hiccup, replays
  // the persisted order instead of opening a second position. It only advances
  // once an order has actually been accepted, which is when the next click
  // genuinely means a new order.
  const intentKey = `ui-${symbol}-${orderNonce}`;

  const canTrade =
    status?.enabled === true &&
    status.connection === "CONNECTED" &&
    account?.can_trade === true;

  const submit = useCallback(async () => {
    setSubmitting(true);
    setActionError(null);
    try {
      const params: TestnetOrderParams = { ...order, symbol, intentKey };
      const result = await submitTestnetOrder(params);
      setOutcome(result);
      // Only a genuinely new order advances the key. A refusal keeps it, so
      // correcting the input and resubmitting is still the same intent.
      if (result.accepted && !result.replayed) setOrderNonce((n) => n + 1);
      refresh();
    } catch (error) {
      setActionError(error instanceof ApiError ? error.message : "The order could not be sent.");
    } finally {
      setSubmitting(false);
    }
  }, [order, symbol, intentKey, refresh]);

  const cancel = useCallback(
    async (orderId: string) => {
      setActionError(null);
      try {
        setOutcome(await cancelTestnetOrder(orderId));
        refresh();
      } catch (error) {
        setActionError(
          error instanceof ApiError ? error.message : "The cancellation could not be sent.",
        );
      }
    },
    [refresh],
  );

  return (
    <main className="tn">
      <header className="tn-header">
        <div>
          <h1>Demo Trading Desk</h1>
          <p className="tn-sub">
            Orders are genuine on Binance&apos;s demo infrastructure. The balance below is that
            venue&apos;s, not your Aetheris paper account.
          </p>
        </div>
        <div className="tn-header-right">
          <span className="tn-env">TESTNET &bull; BINANCE DEMO</span>
          {status ? <ConnectionBadge status={status} /> : null}
        </div>
      </header>

      <p className="tn-warning" role="note">
        SIMULATION ENVIRONMENT — NO REAL FUNDS. This is not production trading and no live
        order can be placed from this page.
      </p>

      {state.kind === "loading" ? <LoadingState label="Reading the demo venue" /> : null}
      {state.kind === "error" ? (
        <ErrorState error={state.error} onRetry={refresh} />
      ) : null}

      {status && !status.enabled ? (
        <section className="tn-panel tn-unavailable">
          <h2>Testnet — Unavailable</h2>
          <p>{status.detail ?? "The backend reports that testnet execution is not configured."}</p>
        </section>
      ) : null}

      {status && status.enabled ? (
        <>
          <section className="tn-panel">
            <h2>Demo Account</h2>
            <div className="tn-grid">
              <Field label="Venue" value={status.venue} />
              <Field
                label="Available balance"
                value={
                  account?.available_balance
                    ? `${formatNumber(account.available_balance)} USDT`
                    : "Unavailable"
                }
              />
              <Field
                label="Trading permission"
                value={
                  account?.can_trade === true
                    ? "Permitted"
                    : account?.can_trade === false
                      ? "Denied by venue"
                      : "Unknown"
                }
                tone={account?.can_trade === true ? "tn-ok" : "tn-warn"}
              />
              <Field label="Position mode" value={account?.position_mode ?? "Unavailable"} />
              <Field label="Margin mode" value={status.margin_mode ?? "Unavailable"} />
              <Field
                label="Last read"
                value={
                  status.observed_at
                    ? new Date(status.observed_at).toLocaleTimeString()
                    : "Unavailable"
                }
              />
            </div>
            {status.detail ? <p className="tn-partial">{status.detail}</p> : null}
          </section>

          <section className="tn-panel">
            <h2>Order Entry</h2>
            <div className="tn-order">
              <label>
                Symbol
                <SymbolSearch value={symbol} onSelect={setSymbol} />
              </label>
              <label>
                Side
                <select
                  value={order.side}
                  onChange={(e) =>
                    setOrder({ ...order, side: e.target.value === "SELL" ? "SELL" : "BUY" })
                  }
                >
                  <option value="BUY">BUY</option>
                  <option value="SELL">SELL</option>
                </select>
              </label>
              <label>
                Margin (USDT)
                <input
                  inputMode="decimal"
                  value={order.margin}
                  onChange={(e) => setOrder({ ...order, margin: e.target.value })}
                />
              </label>
              <label>
                Leverage
                <input
                  inputMode="decimal"
                  value={order.requestedLeverage}
                  onChange={(e) => setOrder({ ...order, requestedLeverage: e.target.value })}
                />
              </label>
              <label>
                Stop loss %
                <input
                  inputMode="decimal"
                  value={order.stopLossPercent}
                  onChange={(e) => setOrder({ ...order, stopLossPercent: e.target.value })}
                />
              </label>
              <button type="button" onClick={submit} disabled={!canTrade || submitting}>
                {submitting ? "Sending…" : "Submit demo order"}
              </button>
            </div>
            <p className="tn-note">
              Leverage is a request, not a permission. The backend takes the lower of your
              request, the venue&apos;s ceiling for this order&apos;s size, and the risk
              engine&apos;s own limit — and refuses outright if any of them is unknown.
            </p>
            {!canTrade ? (
              <p className="tn-partial">
                Order entry is disabled: the backend has not confirmed a connected, trading-
                permitted demo account.
              </p>
            ) : null}
            {actionError ? <p className="tn-bad-text">{actionError}</p> : null}
            {outcome ? (
              <div className={outcome.accepted ? "tn-outcome tn-ok" : "tn-outcome tn-bad"}>
                <strong>{outcome.accepted ? "Accepted" : "Refused"}</strong>
                {outcome.replayed ? " (replayed an existing order)" : null}
                <p>{outcome.detail}</p>
                {outcome.rejection_code ? <code>{outcome.rejection_code}</code> : null}
                {outcome.venue_leverage ? (
                  <p className="tn-note">
                    Venue confirmed {outcome.venue_leverage}x, {outcome.venue_margin_mode} margin.
                  </p>
                ) : null}
              </div>
            ) : null}
          </section>

          <section className="tn-panel">
            <h2>Positions</h2>
            {status.positions.length === 0 ? (
              <p className="tn-empty">No open positions on the demo account.</p>
            ) : (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Symbol</th>
                      <th>Side</th>
                      <th>Size</th>
                      <th>Entry</th>
                      <th>Mark</th>
                      <th>Unrealised</th>
                      <th>Margin</th>
                      <th>Leverage</th>
                    </tr>
                  </thead>
                  <tbody>
                    {status.positions.map((p) => (
                      <PositionRow key={p.symbol} position={p} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="tn-panel">
            <h2>Open Orders</h2>
            {status.open_orders.length === 0 ? (
              <p className="tn-empty">No open orders for {symbol}.</p>
            ) : (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Client ID</th>
                      <th>Venue ID</th>
                      <th>State</th>
                      <th>Filled</th>
                      <th>Avg price</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {status.open_orders.map((o) => (
                      <tr key={o.client_order_id}>
                        <td className="mono">{o.client_order_id}</td>
                        <td className="mono">{o.venue_order_id ?? NO_VALUE}</td>
                        <td>{o.state}</td>
                        <td className="num">{formatNumber(o.filled_quantity)}</td>
                        <td className="num">
                          {o.average_fill_price ? formatPrice(o.average_fill_price) : NO_VALUE}
                        </td>
                        <td>
                          <button
                            type="button"
                            onClick={() => cancel(o.client_order_id)}
                            disabled={!canTrade}
                          >
                            Cancel
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      ) : null}
    </main>
  );
}
