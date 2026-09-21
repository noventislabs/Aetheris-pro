"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ErrorState, LoadingState } from "@/components/DataState";
import { SymbolSearch } from "@/components/SymbolSearch";
import {
  ApiError,
  closePaperPosition,
  getPaperAccount,
  getPaperMethod,
  resetPaperAccount,
  setPaperEmergencyStop,
  submitPaperOrder,
  tickPaper,
  type PaperOrderParams,
} from "@/lib/api";
import { changeDirection, formatNumber, formatPrice, NO_VALUE } from "@/lib/format";
import type {
  PaperAccount,
  PaperOrder,
  PaperOrderResult,
  PaperPosition,
  PaperTrade,
} from "@/lib/types";
import { useApiResource } from "@/lib/useApiResource";

/**
 * Paper trading desk.
 *
 * Every element here exists to keep one distinction impossible to lose: this
 * is a simulation running on real prices, not a trading terminal. The mode
 * banner, the in-memory warning and the disclaimer are permanent fixtures
 * rather than a dismissible notice, because the moment a user forgets which
 * mode they are in is the moment the numbers start meaning something they do
 * not mean.
 *
 * Refusals are rendered as prominently as fills. A risk limit that fires
 * silently teaches a user that limits do not exist.
 */

const DEFAULT_ORDER: PaperOrderParams = {
  symbol: "BTCUSDT",
  side: "BUY",
  margin: "20",
  leverage: "1",
  stopLossPercent: "2",
  takeProfitPercent: "4",
};

/** How often the management pass runs while the page is open. */
const TICK_INTERVAL_MS = 5000;

function Stat({
  label,
  value,
  tone,
  title,
}: {
  label: string;
  value: string;
  tone?: "up" | "down" | "flat";
  title?: string;
}) {
  return (
    <div className="stat" title={title}>
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone ?? ""}`}>{value}</div>
    </div>
  );
}

function AccountStats({ account }: { account: PaperAccount }) {
  const realisedTone = changeDirection(account.realized_pnl);
  return (
    <div className="stats">
      <Stat label="Equity" value={formatNumber(account.equity, 4)} />
      <Stat
        label="Balance"
        value={formatNumber(account.balance, 4)}
        title="Realised cash. Unrealised PnL is not folded in."
      />
      <Stat
        label="Available"
        value={formatNumber(account.available_balance, 4)}
        title="Balance not posted as margin. Unrealised profit does not fund new positions here."
      />
      <Stat label="Margin used" value={formatNumber(account.margin_used, 4)} />
      <Stat label="Realised PnL" value={formatNumber(account.realized_pnl, 4)} tone={realisedTone} />
      <Stat
        label="Unrealised PnL"
        value={
          account.unrealized_pnl === null ? NO_VALUE : formatNumber(account.unrealized_pnl, 4)
        }
        tone={
          account.unrealized_pnl === null ? undefined : changeDirection(account.unrealized_pnl)
        }
        title={
          account.unrealized_pnl === null
            ? "An open position could not be marked to a usable price. Not zero — unknown."
            : "Marked to the latest observed price"
        }
      />
      <Stat label="Fees paid" value={formatNumber(account.total_fees, 4)} tone="down" />
      <Stat
        label="Day realised"
        value={formatNumber(account.session.realized_pnl, 4)}
        tone={changeDirection(account.session.realized_pnl)}
        title={`Target ${account.session.profit_target} · limit ${account.session.loss_limit}`}
      />
    </div>
  );
}

function PositionRow({
  position,
  onClose,
  busy,
}: {
  position: PaperPosition;
  onClose: (symbol: string) => void;
  busy: boolean;
}) {
  const pnl = position.unrealized_pnl;
  return (
    <tr>
      <td>{position.symbol}</td>
      <td className={position.side === "LONG" ? "up" : "down"}>{position.side}</td>
      <td className="num">{position.quantity}</td>
      <td className="num">{formatPrice(position.entry_price)}</td>
      <td className="num">
        {position.mark_price === null ? (
          <span title={`Mark unavailable (${position.mark_status ?? "UNKNOWN"})`}>{NO_VALUE}</span>
        ) : (
          formatPrice(position.mark_price)
        )}
      </td>
      <td className={`num ${pnl === null ? "" : changeDirection(pnl)}`}>
        {pnl === null ? NO_VALUE : formatNumber(pnl, 4)}
      </td>
      <td className="num">{formatNumber(position.margin, 2)}</td>
      <td className="num">{position.approved_leverage}x</td>
      <td className="num">
        {position.stop_price ? formatPrice(position.stop_price) : NO_VALUE}
      </td>
      <td className="num">
        {position.target_price ? formatPrice(position.target_price) : NO_VALUE}
      </td>
      <td className="num" title="Null at 1x long: a level of zero is not a liquidation price">
        {position.liquidation_price ? formatPrice(position.liquidation_price) : NO_VALUE}
      </td>
      <td>
        <button
          type="button"
          className="run-button"
          disabled={busy}
          onClick={() => onClose(position.symbol)}
        >
          Close
        </button>
      </td>
    </tr>
  );
}

/**
 * Card fallbacks for narrow viewports.
 *
 * Below 720px the stylesheet hides `.table-scroll` and shows `.cards`, because
 * a twelve-column table is unusable at 390px. A table with no card beside it
 * therefore renders nothing at all -- which is exactly what happened here until
 * a browser check at 390px caught the positions table collapsing to zero
 * height. Every table on this page now has a card.
 */

function Field({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div>
      <span>{label}</span>
      <span className={`num ${tone ?? ""}`}>{value}</span>
    </div>
  );
}

function PositionCard({
  position,
  onClose,
  busy,
}: {
  position: PaperPosition;
  onClose: (symbol: string) => void;
  busy: boolean;
}) {
  const pnl = position.unrealized_pnl;
  return (
    <article className="card">
      <div className="card-top">
        <span style={{ fontWeight: 600 }}>
          {position.symbol}{" "}
          <span className={position.side === "LONG" ? "up" : "down"}>{position.side}</span>
        </span>
        <span className={`num ${pnl === null ? "" : changeDirection(pnl)}`}>
          {pnl === null ? NO_VALUE : formatNumber(pnl, 4)}
        </span>
      </div>
      <div className="card-grid">
        <Field label="Qty" value={position.quantity} />
        <Field label="Entry" value={formatPrice(position.entry_price)} />
        <Field
          label="Mark"
          value={position.mark_price === null ? NO_VALUE : formatPrice(position.mark_price)}
        />
        <Field label="Margin" value={formatNumber(position.margin, 2)} />
        <Field label="Leverage" value={`${position.approved_leverage}x`} />
        <Field
          label="Stop"
          value={position.stop_price ? formatPrice(position.stop_price) : NO_VALUE}
        />
        <Field
          label="Target"
          value={position.target_price ? formatPrice(position.target_price) : NO_VALUE}
        />
        <Field
          label="Liquidation"
          value={position.liquidation_price ? formatPrice(position.liquidation_price) : NO_VALUE}
        />
      </div>
      <button
        type="button"
        className="run-button"
        disabled={busy}
        onClick={() => onClose(position.symbol)}
      >
        Close {position.symbol}
      </button>
    </article>
  );
}

function TradeCard({ trade }: { trade: PaperTrade }) {
  return (
    <article className="card">
      <div className="card-top">
        <span style={{ fontWeight: 600 }}>
          {trade.symbol}{" "}
          <span className={trade.side === "LONG" ? "up" : "down"}>{trade.side}</span>{" "}
          <span className="tag">{trade.exit_reason}</span>
        </span>
        <span className={`num ${changeDirection(trade.net_pnl)}`}>
          {formatNumber(trade.net_pnl, 4)}
        </span>
      </div>
      <div className="card-grid">
        <Field label="Entry" value={formatPrice(trade.entry_price)} />
        <Field label="Exit" value={formatPrice(trade.exit_price)} />
        <Field
          label="Return"
          value={`${formatNumber(trade.return_percent, 2)}%`}
          tone={changeDirection(trade.return_percent)}
        />
        <Field label="Fees" value={formatNumber(trade.fees, 4)} />
      </div>
    </article>
  );
}

function OrderCard({ order }: { order: PaperOrder }) {
  const refused = order.state === "REJECTED";
  return (
    <article className="card">
      <div className="card-top">
        <span style={{ fontWeight: 600 }}>
          {order.symbol} {order.side}
        </span>
        <span className={refused ? "down" : "up"}>{order.state}</span>
      </div>
      <div className="card-grid">
        <Field label="Qty" value={refused ? NO_VALUE : order.filled_quantity} />
        <Field
          label="Price"
          value={order.average_fill_price ? formatPrice(order.average_fill_price) : NO_VALUE}
        />
      </div>
      {order.rejection_code ? (
        <p className="state-detail">
          <strong>{order.rejection_code}</strong>
          {order.rejection_detail ? ` -- ${order.rejection_detail}` : null}
        </p>
      ) : null}
    </article>
  );
}

export default function PaperPage() {
  const [form, setForm] = useState<PaperOrderParams>(DEFAULT_ORDER);
  const [outcome, setOutcome] = useState<PaperOrderResult | null>(null);
  const [account, setAccount] = useState<PaperAccount | null>(null);
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const initial = useApiResource((signal) => getPaperAccount(signal), []);
  const method = useApiResource((signal) => getPaperMethod(signal), []);

  useEffect(() => {
    if (initial.state.kind === "success") setAccount(initial.state.data);
  }, [initial.state]);

  const update = useCallback(
    <K extends keyof PaperOrderParams>(key: K, value: PaperOrderParams[K]) =>
      setForm((current) => ({ ...current, [key]: value })),
    [],
  );

  const act = useCallback(async <T,>(run: () => Promise<T>, apply: (result: T) => void) => {
    setBusy(true);
    setActionError(null);
    try {
      const result = await run();
      if (mounted.current) apply(result);
    } catch (error) {
      if (mounted.current) {
        setActionError(
          error instanceof ApiError
            ? error
            : new ApiError("The request failed", "UNKNOWN", 0),
        );
      }
    } finally {
      if (mounted.current) setBusy(false);
    }
  }, []);

  // The management pass is poll-driven by design, so the page drives it. A
  // stop is evaluated when this fires, not the instant price touches it.
  //
  // The effect depends on `ready`, not on `account`: depending on the account
  // object would tear down and rebuild the interval on every tick, restarting
  // the clock each time and making the real cadence something other than the
  // one this constant declares.
  const ready = account !== null;
  useEffect(() => {
    if (!ready) return;
    const timer = setInterval(() => {
      void tickPaper()
        .then((result) => {
          if (mounted.current) setAccount(result.account);
        })
        .catch(() => {
          // A failed tick leaves the last known account on screen and is
          // reported by the next explicit action. Blanking the desk because
          // one poll failed would be worse than showing state one poll old.
        });
    }, TICK_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [ready]);

  const submit = () =>
    void act(
      () => submitPaperOrder(form),
      (result) => {
        setOutcome(result);
        setAccount(result.account);
      },
    );

  const close = (symbol: string) =>
    void act(
      () => closePaperPosition(symbol),
      (result) => {
        setOutcome(result);
        setAccount(result.account);
      },
    );

  const reset = () => void act(() => resetPaperAccount(), setAccount);

  const emergencyStop = (engaged: boolean) =>
    void act(
      () => setPaperEmergencyStop(engaged, "Engaged from the paper trading desk"),
      setAccount,
    );

  const lock = account?.session.lock_state ?? "NONE";
  const methodData = method.state.kind === "success" ? method.state.data : null;

  return (
    <div className="grid-2">
      <section className="panel">
        <div className="panel-head">
          <h1 className="panel-title">Paper trading</h1>
          <span className="badge">PAPER / SIMULATION ONLY / NO REAL ORDER</span>
        </div>

        <p className="notice" role="note">
          <strong>PAPER STATE: IN-MEMORY — RESETS ON RESTART.</strong> Balances, positions
          and history exist only in the running backend process and are lost when it
          stops. Durable storage needs the database from phase 1.
        </p>

        {initial.state.kind === "loading" ? (
          <LoadingState label="Loading account" />
        ) : initial.state.kind === "error" ? (
          <ErrorState error={initial.state.error} onRetry={initial.refresh} />
        ) : account ? (
          <>
            {lock !== "NONE" ? (
              <p className="notice" role="status">
                <strong>{lock.replace(/_/g, " ")}</strong> —{" "}
                {account.session.lock_reason ?? "New entries are blocked."} Open positions
                are still managed and are never force-closed.
              </p>
            ) : null}
            <AccountStats account={account} />
            <div className="bt-row">
              <button type="button" className="run-button" disabled={busy} onClick={reset}>
                Reset account
              </button>
              <button
                type="button"
                className="run-button"
                disabled={busy}
                onClick={() => emergencyStop(lock !== "EMERGENCY_STOP")}
              >
                {lock === "EMERGENCY_STOP" ? "Release stop" : "Emergency stop"}
              </button>
              <span className="freshness">
                {account.session.orders_submitted} filled ·{" "}
                {account.session.orders_rejected} refused today
              </span>
            </div>
          </>
        ) : null}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">New paper order</h2>
        </div>

        <form
          className="bt-form"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <div className="bt-row">
            <SymbolSearch value={form.symbol} onSelect={(symbol) => update("symbol", symbol)} />
            <div className="segmented" role="group" aria-label="Side">
              {(["BUY", "SELL"] as const).map((side) => (
                <button
                  key={side}
                  type="button"
                  aria-pressed={form.side === side}
                  onClick={() => update("side", side)}
                >
                  {side}
                </button>
              ))}
            </div>
            <label className="field">
              <span>Margin</span>
              <input
                className="input"
                type="number"
                step="any"
                value={form.margin ?? ""}
                title="USDT to commit to this position"
                onChange={(event) => update("margin", event.target.value)}
              />
            </label>
            <label className="field">
              <span>Leverage</span>
              <input
                className="input"
                type="number"
                step="any"
                min={1}
                max={500}
                value={form.leverage}
                title="A request, not an authorisation. Anything above 1x is refused in this build: the venue's per-symbol ceiling needs an authenticated endpoint and the risk engine is phase 7."
                onChange={(event) => update("leverage", event.target.value)}
              />
            </label>
          </div>

          <div className="bt-row">
            <label className="field">
              <span>Stop %</span>
              <input
                className="input"
                type="number"
                step="any"
                placeholder="off"
                value={form.stopLossPercent ?? ""}
                onChange={(event) =>
                  update("stopLossPercent", event.target.value.trim() || undefined)
                }
              />
            </label>
            <label className="field">
              <span>Target %</span>
              <input
                className="input"
                type="number"
                step="any"
                placeholder="off"
                value={form.takeProfitPercent ?? ""}
                onChange={(event) =>
                  update("takeProfitPercent", event.target.value.trim() || undefined)
                }
              />
            </label>
            <label className="field">
              <span>Trailing %</span>
              <input
                className="input"
                type="number"
                step="any"
                placeholder="off"
                value={form.trailingStopPercent ?? ""}
                onChange={(event) =>
                  update("trailingStopPercent", event.target.value.trim() || undefined)
                }
              />
            </label>
            <button type="submit" className="run-button" disabled={busy}>
              Submit paper order
            </button>
          </div>
        </form>

        {actionError ? <ErrorState error={actionError} /> : null}

        {outcome ? (
          <div className="state" role="status">
            <span className="state-code">
              {outcome.accepted ? "FILLED" : (outcome.order.rejection_code ?? "REFUSED")}
            </span>
            <div>{outcome.detail}</div>
            {outcome.order.leverage ? (
              <p className="state-detail">
                Leverage requested {outcome.order.leverage.requested_leverage ?? NO_VALUE}x ·
                venue ceiling {outcome.order.leverage.exchange_max_leverage ?? "UNKNOWN"} ·
                risk ceiling {outcome.order.leverage.risk_max_leverage ?? "UNKNOWN"} ·
                approved {outcome.order.leverage.approved_leverage ?? NO_VALUE}
              </p>
            ) : null}
            {outcome.order.fills[0] ? (
              <p className="state-detail">
                Filled at {formatPrice(outcome.order.fills[0].price)} from{" "}
                {outcome.order.fills[0].price_source}
              </p>
            ) : null}
          </div>
        ) : null}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Open positions ({account?.positions.length ?? 0})</h2>
          <span className="freshness">
            Managed every {TICK_INTERVAL_MS / 1000}s while this page is open
          </span>
        </div>
        <p className="notice" role="note">
          <strong>Management runs only while this page is open.</strong> There is no
          server-side loop: stops, targets, trailing stops and liquidation are evaluated
          when this page polls. Close the tab and nothing is evaluated until you return.
          A level is also noticed on a poll rather than the instant it is touched, so a
          fill uses the price observed then — which can be past the level, and means a
          realised loss can exceed the stop distance.
        </p>
        {!account || account.positions.length === 0 ? (
          <div className="state">
            <span className="state-code">FLAT</span>
            <div>No simulated position is open.</div>
          </div>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th className="num">Qty</th>
                  <th className="num">Entry</th>
                  <th className="num">Mark</th>
                  <th className="num">Unrealised</th>
                  <th className="num">Margin</th>
                  <th className="num">Lev</th>
                  <th className="num">Stop</th>
                  <th className="num">Target</th>
                  <th className="num">Liq</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {account.positions.map((position) => (
                  <PositionRow
                    key={position.position_id}
                    position={position}
                    onClose={close}
                    busy={busy}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
        {account && account.positions.length > 0 ? (
          <div className="cards">
            {account.positions.map((position) => (
              <PositionCard
                key={position.position_id}
                position={position}
                onClose={close}
                busy={busy}
              />
            ))}
          </div>
        ) : null}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Closed trades ({account?.recent_trades.length ?? 0})</h2>
        </div>
        {!account || account.recent_trades.length === 0 ? (
          <div className="state">
            <span className="state-code">NO TRADES</span>
            <div>Nothing has been closed in this session.</div>
          </div>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th>Exit</th>
                  <th className="num">Entry</th>
                  <th className="num">Exit price</th>
                  <th className="num">Net PnL</th>
                  <th className="num">Return</th>
                  <th className="num">Fees</th>
                </tr>
              </thead>
              <tbody>
                {account.recent_trades.map((trade) => (
                  <tr key={trade.trade_id}>
                    <td>{trade.symbol}</td>
                    <td className={trade.side === "LONG" ? "up" : "down"}>{trade.side}</td>
                    <td>{trade.exit_reason}</td>
                    <td className="num">{formatPrice(trade.entry_price)}</td>
                    <td className="num">{formatPrice(trade.exit_price)}</td>
                    <td className={`num ${changeDirection(trade.net_pnl)}`}>
                      {formatNumber(trade.net_pnl, 4)}
                    </td>
                    <td className={`num ${changeDirection(trade.return_percent)}`}>
                      {formatNumber(trade.return_percent, 2)}%
                    </td>
                    <td className="num">{formatNumber(trade.fees, 4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {account && account.recent_trades.length > 0 ? (
          <div className="cards">
            {account.recent_trades.map((trade) => (
              <TradeCard key={trade.trade_id} trade={trade} />
            ))}
          </div>
        ) : null}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Order history ({account?.recent_orders.length ?? 0})</h2>
          <span className="freshness">Refusals are kept, not hidden</span>
        </div>
        {!account || account.recent_orders.length === 0 ? (
          <div className="state">
            <span className="state-code">EMPTY</span>
            <div>No order has been submitted yet.</div>
          </div>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th>State</th>
                  <th className="num">Qty</th>
                  <th className="num">Price</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {account.recent_orders.map((order) => (
                  <tr key={order.order_id}>
                    <td>{order.symbol}</td>
                    <td>{order.side}</td>
                    <td className={order.state === "REJECTED" ? "down" : "up"}>{order.state}</td>
                    {/*
                      A refused order has no resolved quantity -- the size was
                      never accepted. Showing the nominal placeholder as "0"
                      would read as an order for nothing rather than an order
                      that was turned away.
                    */}
                    <td className="num">
                      {order.state === "REJECTED" ? NO_VALUE : order.filled_quantity}
                    </td>
                    <td className="num">
                      {order.average_fill_price ? formatPrice(order.average_fill_price) : NO_VALUE}
                    </td>
                    <td title={order.rejection_detail ?? undefined}>
                      {order.rejection_code ?? NO_VALUE}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {account && account.recent_orders.length > 0 ? (
          <div className="cards">
            {account.recent_orders.map((order) => (
              <OrderCard key={order.order_id} order={order} />
            ))}
          </div>
        ) : null}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">What this simulation does and does not model</h2>
        </div>
        {methodData ? (
          <>
            <p className="notice">{methodData.disclaimer}</p>
            <ul className="assumption-list">
              {methodData.assumptions.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
            <p className="state-detail">
              <strong>Not modelled:</strong> {methodData.not_modelled.join(" · ")}
            </p>
          </>
        ) : (
          <LoadingState label="Loading method" />
        )}
      </section>
    </div>
  );
}
