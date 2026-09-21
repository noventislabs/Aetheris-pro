"use client";

import { useCallback, useState } from "react";
import { ErrorState, LoadingState } from "@/components/DataState";
import { EquityCurve } from "@/components/EquityCurve";
import { SymbolSearch } from "@/components/SymbolSearch";
import { runBacktest, type BacktestParams } from "@/lib/api";
import { changeDirection, formatNumber, formatPrice, NO_VALUE } from "@/lib/format";
import { TIMEFRAMES, type BacktestResult, type Timeframe } from "@/lib/types";
import { useApiResource } from "@/lib/useApiResource";

/**
 * Backtest dashboard.
 *
 * The page is built so a number cannot be read without its context. The
 * HISTORICAL SIMULATION label, the warnings, and the list of assumptions are
 * not tucked into a tooltip — they sit next to the metrics, because a profit
 * factor without its fill model is not interpretable.
 *
 * Nothing runs automatically. A backtest is the most expensive request the
 * backend serves, and polling it would spend the venue's rate-limit budget on
 * a result nobody asked to refresh.
 */

const DEFAULTS: BacktestParams = {
  timeframe: "1h",
  limit: 500,
  startingBalance: "100",
  positionSizePercent: "10",
  leverage: "1",
  feeBps: "5",
  slippageBps: "2",
  stopLossPercent: "2",
  takeProfitPercent: "4",
  trailingStopPercent: null,
  allowLong: true,
  allowShort: true,
};

function Metric({
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

function Metrics({ result }: { result: BacktestResult }) {
  const m = result.metrics;
  if (!m) return null;
  const returnTone = changeDirection(m.return_percent);

  return (
    <div className="stats">
      <Metric label="Net PnL" value={formatNumber(m.net_pnl, 4)} tone={returnTone} />
      <Metric label="Return" value={`${formatNumber(m.return_percent, 2)}%`} tone={returnTone} />
      <Metric label="Trades" value={String(m.total_trades)} />
      <Metric
        label="Win rate"
        value={m.win_rate_percent ? `${formatNumber(m.win_rate_percent, 1)}%` : NO_VALUE}
        title={`${m.winning_trades}W / ${m.losing_trades}L / ${m.breakeven_trades}BE`}
      />
      <Metric
        label="Profit factor"
        value={m.profit_factor ? formatNumber(m.profit_factor, 2) : NO_VALUE}
        title={
          m.profit_factor
            ? "Gross profit divided by gross loss"
            : "Undefined: there were no losing trades, so the ratio has no denominator"
        }
      />
      <Metric
        label="Max drawdown"
        value={`${formatNumber(m.max_drawdown_percent, 2)}%`}
        tone="down"
        title={`${formatNumber(m.max_drawdown_absolute, 4)} below the running peak`}
      />
      <Metric
        label="Sharpe-like"
        value={m.sharpe_like_ratio ? formatNumber(m.sharpe_like_ratio, 2) : NO_VALUE}
        title={
          m.sharpe_like_ratio
            ? "Annualised mean over standard deviation of per-bar returns, zero risk-free rate. Not the textbook Sharpe ratio."
            : "Undefined: the equity curve has no dispersion to divide by"
        }
      />
      <Metric label="Exposure" value={`${formatNumber(m.exposure_percent, 1)}%`} />
      <Metric label="Fees paid" value={formatNumber(m.total_fees, 4)} tone="down" />
      <Metric label="Avg trade" value={m.average_trade ? formatNumber(m.average_trade, 4) : NO_VALUE} />
      <Metric label="Bars" value={String(m.bars_tested)} />
      <Metric
        label="Balance"
        value={`${formatNumber(m.starting_balance, 2)} → ${formatNumber(m.ending_balance, 2)}`}
      />
    </div>
  );
}

export default function BacktestPage() {
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [form, setForm] = useState<BacktestParams>(DEFAULTS);
  const [submitted, setSubmitted] = useState<{ symbol: string; params: BacktestParams } | null>(
    null,
  );

  const update = useCallback(
    <K extends keyof BacktestParams>(key: K, value: BacktestParams[K]) =>
      setForm((current) => ({ ...current, [key]: value })),
    [],
  );

  const run = useApiResource(
    (signal) =>
      submitted
        ? runBacktest(submitted.symbol, submitted.params, signal)
        : Promise.reject(new Error("not requested")),
    [submitted],
    { enabled: submitted !== null },
  );

  const result = run.state.kind === "success" ? run.state.data : null;

  const numberField = (
    label: string,
    key: "startingBalance" | "positionSizePercent" | "leverage" | "feeBps" | "slippageBps",
    hint?: string,
  ) => (
    <label className="field">
      <span>{label}</span>
      <input
        className="input"
        type="number"
        step="any"
        value={form[key]}
        title={hint}
        onChange={(event) => update(key, event.target.value)}
      />
    </label>
  );

  const optionalField = (
    label: string,
    key: "stopLossPercent" | "takeProfitPercent" | "trailingStopPercent",
  ) => (
    <label className="field">
      <span>{label}</span>
      <input
        className="input"
        type="number"
        step="any"
        placeholder="off"
        value={form[key] ?? ""}
        onChange={(event) => update(key, event.target.value.trim() || null)}
      />
    </label>
  );

  return (
    <div className="grid-2">
      <section className="panel">
        <div className="panel-head">
          <h1 className="panel-title">Backtest</h1>
          <span className="badge">HISTORICAL SIMULATION</span>
        </div>

        <form
          className="bt-form"
          onSubmit={(event) => {
            event.preventDefault();
            setSubmitted({ symbol, params: form });
          }}
        >
          <div className="bt-row">
            <SymbolSearch value={symbol} onSelect={setSymbol} />
            <div className="segmented" role="group" aria-label="Timeframe">
              {TIMEFRAMES.map((frame) => (
                <button
                  key={frame}
                  type="button"
                  aria-pressed={frame === form.timeframe}
                  onClick={() => update("timeframe", frame as Timeframe)}
                >
                  {frame}
                </button>
              ))}
            </div>
            <label className="field">
              <span>Candles</span>
              <input
                className="input"
                type="number"
                min={60}
                max={1500}
                value={form.limit}
                onChange={(event) => update("limit", Number(event.target.value))}
              />
            </label>
          </div>

          <div className="bt-row">
            {numberField("Balance", "startingBalance")}
            {numberField("Size %", "positionSizePercent", "Percent of equity posted as margin")}
            {numberField(
              "Leverage",
              "leverage",
              "Simulation input only. It authorises nothing and sets no leverage on any venue.",
            )}
            {numberField("Fee bps", "feeBps", "Taker fee per side, in basis points")}
            {numberField("Slippage bps", "slippageBps", "Adverse movement on every fill")}
          </div>

          <div className="bt-row">
            {optionalField("Stop %", "stopLossPercent")}
            {optionalField("Target %", "takeProfitPercent")}
            {optionalField("Trailing %", "trailingStopPercent")}
            <label className="field-check">
              <input
                type="checkbox"
                checked={form.allowLong}
                onChange={(event) => update("allowLong", event.target.checked)}
              />
              Longs
            </label>
            <label className="field-check">
              <input
                type="checkbox"
                checked={form.allowShort}
                onChange={(event) => update("allowShort", event.target.checked)}
              />
              Shorts
            </label>
            <button type="submit" className="run-button">
              Run simulation
            </button>
          </div>
        </form>

        {submitted === null ? (
          <div className="state">
            <span className="state-code">READY</span>
            <div>Choose an instrument and settings, then run the simulation.</div>
            <p className="state-detail">
              Nothing runs automatically: a backtest is the most expensive request the
              backend serves.
            </p>
          </div>
        ) : run.state.kind === "loading" ? (
          <LoadingState label="Simulating" />
        ) : run.state.kind === "error" ? (
          <ErrorState error={run.state.error} onRetry={run.refresh} />
        ) : result && result.status !== "COMPLETED" ? (
          <div className="state" role="status">
            <span className="state-code">{result.status}</span>
            <div>{result.detail ?? "The simulation could not be run."}</div>
          </div>
        ) : result ? (
          <>
            <div className="panel-head">
              <span className="freshness">
                <span>
                  {result.symbol} · {result.timeframe} · {result.strategy} v
                  {result.strategy_version}
                </span>
                <span aria-hidden="true">·</span>
                <span>{result.source}</span>
              </span>
              <span className="freshness">
                {result.first_bar_time ? new Date(result.first_bar_time).toLocaleDateString("en-GB") : ""}
                {" → "}
                {result.last_bar_time ? new Date(result.last_bar_time).toLocaleDateString("en-GB") : ""}
              </span>
            </div>
            <Metrics result={result} />
          </>
        ) : null}
      </section>

      {result && result.status === "COMPLETED" ? (
        <>
          <section className="panel">
            <div className="panel-head">
              <h2 className="panel-title">Equity and drawdown</h2>
              <span className="freshness">{result.equity_curve.length} points</span>
            </div>
            <EquityCurve
              points={result.equity_curve}
              startingBalance={result.config.starting_balance}
            />
            {result.warnings.map((warning) => (
              <p key={warning} className="notice">
                <strong>Warning</strong> — {warning}
              </p>
            ))}
          </section>

          <section className="panel">
            <div className="panel-head">
              <h2 className="panel-title">Trades ({result.trades.length})</h2>
            </div>
            {result.trades.length === 0 ? (
              <div className="state">
                <span className="state-code">NO TRADES</span>
                <div>The rules never aligned over this window.</div>
              </div>
            ) : (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Entry</th>
                      <th scope="col">Side</th>
                      <th scope="col">Entry px</th>
                      <th scope="col">Exit px</th>
                      <th scope="col">Bars</th>
                      <th scope="col">Exit</th>
                      <th scope="col">Fees</th>
                      <th scope="col">Net PnL</th>
                      <th scope="col">Return</th>
                      <th scope="col">Equity</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.trades.map((trade) => (
                      <tr key={`${trade.entry_time}-${trade.exit_time}`}>
                        <td>{new Date(trade.entry_time).toLocaleString("en-GB", { hour12: false })}</td>
                        <td className={trade.side === "LONG" ? "up" : "down"}>{trade.side}</td>
                        <td className="num">{formatPrice(trade.entry_price)}</td>
                        <td className="num">{formatPrice(trade.exit_price)}</td>
                        <td className="num">{trade.bars_held}</td>
                        <td>
                          <span className="badge">{trade.exit_reason}</span>
                        </td>
                        <td className="num down">{formatNumber(trade.fees, 4)}</td>
                        <td className={`num ${changeDirection(trade.net_pnl)}`}>
                          {formatNumber(trade.net_pnl, 4)}
                        </td>
                        <td className={`num ${changeDirection(trade.return_percent)}`}>
                          {formatNumber(trade.return_percent, 2)}%
                        </td>
                        <td className="num">{formatNumber(trade.equity_after, 2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="panel">
            <div className="panel-head">
              <h2 className="panel-title">Assumptions</h2>
              <span className="badge badge-warn">{result.label}</span>
            </div>
            <ul className="assumption-list">
              {result.assumptions.map((assumption) => (
                <li key={assumption}>{assumption}</li>
              ))}
            </ul>
            <p className="notice">{result.disclaimer}</p>
          </section>
        </>
      ) : null}
    </div>
  );
}
