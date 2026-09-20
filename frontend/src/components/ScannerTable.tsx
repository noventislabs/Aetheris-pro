"use client";

import { StatusPill } from "@/components/Freshness";
import { ScoreCell } from "@/components/ScoreCell";
import { changeDirection, formatCompact, formatNumber, formatPercent, formatPrice, NO_VALUE } from "@/lib/format";
import type { ScannerRow, ScannerSortField } from "@/lib/types";

/**
 * Scanner results.
 *
 * Rendered twice, deliberately: a dense table on wide viewports and a card list
 * below 720px. A 10-column table at 390px is either unreadable or scrolls the
 * page sideways, and the cards carry the same fields rather than a reduced set.
 *
 * A row whose ticker is stale shows the STATUS pill and em dashes — the backend
 * withheld the numbers, so there is nothing to display and nothing to invent.
 */

interface Column {
  key: ScannerSortField;
  label: string;
  sortable: boolean;
}

const COLUMNS: Column[] = [
  { key: "symbol", label: "Symbol", sortable: true },
  { key: "last_price", label: "Price", sortable: true },
  { key: "price_change_percent_24h", label: "24h %", sortable: true },
  { key: "quote_volume_24h", label: "Quote Vol", sortable: true },
  { key: "volatility_percent", label: "Volatility", sortable: true },
  { key: "atr_percent", label: "ATR %", sortable: true },
  { key: "momentum_percent", label: "Momentum", sortable: true },
  { key: "relative_volume", label: "Rel Vol", sortable: true },
  { key: "trend_consistency", label: "Trend", sortable: true },
  { key: "opportunity_score", label: "Opportunity", sortable: true },
];

function trendClass(row: ScannerRow): string {
  const trend = row.metrics?.trend;
  if (trend === "UP") return "up";
  if (trend === "DOWN") return "down";
  return "flat";
}

function metricText(row: ScannerRow, pick: (m: NonNullable<ScannerRow["metrics"]>) => string | null, decimals = 2): string {
  if (!row.metrics) return NO_VALUE;
  return formatNumber(pick(row.metrics), decimals);
}

export function ScannerTable({
  rows,
  sortBy,
  direction,
  onSort,
  onSelectSymbol,
}: {
  rows: readonly ScannerRow[];
  sortBy: string;
  direction: "asc" | "desc";
  onSort: (field: ScannerSortField) => void;
  onSelectSymbol?: (symbol: string) => void;
}) {
  const arrow = (key: string) => (key === sortBy ? (direction === "desc" ? "↓" : "↑") : "");

  return (
    <>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {COLUMNS.map((column) => (
                <th
                  key={column.key}
                  scope="col"
                  aria-sort={
                    column.key === sortBy
                      ? direction === "desc"
                        ? "descending"
                        : "ascending"
                      : "none"
                  }
                >
                  {column.sortable ? (
                    <button type="button" onClick={() => onSort(column.key)}>
                      {column.label}
                      <span aria-hidden="true">{arrow(column.key)}</span>
                    </button>
                  ) : (
                    column.label
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const stale = row.ticker_status !== "OK";
              return (
                <tr key={row.symbol} className={stale ? "row-muted" : undefined}>
                  <td>
                    <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                      {onSelectSymbol ? (
                        <button
                          type="button"
                          onClick={() => onSelectSymbol(row.symbol)}
                          style={{ color: "inherit", fontWeight: 500 }}
                          title={`Open ${row.symbol} in the markets terminal`}
                        >
                          {row.symbol}
                        </button>
                      ) : (
                        row.symbol
                      )}
                      <StatusPill status={row.ticker_status} />
                    </span>
                  </td>
                  <td className="num">{formatPrice(row.last_price)}</td>
                  <td className={`num ${changeDirection(row.price_change_percent_24h)}`}>
                    {formatPercent(row.price_change_percent_24h)}
                  </td>
                  <td className="num">{formatCompact(row.quote_volume_24h)}</td>
                  <td className="num">{metricText(row, (m) => m.volatility_percent)}</td>
                  <td className="num">{metricText(row, (m) => m.atr_percent)}</td>
                  <td className={`num ${row.metrics ? changeDirection(row.metrics.momentum_percent) : "flat"}`}>
                    {row.metrics ? formatPercent(row.metrics.momentum_percent) : NO_VALUE}
                  </td>
                  <td className="num">{metricText(row, (m) => m.relative_volume)}</td>
                  <td className={`num ${trendClass(row)}`}>
                    {row.metrics ? `${row.metrics.trend} ${formatNumber(row.metrics.trend_consistency, 2)}` : NO_VALUE}
                  </td>
                  <td>
                    <ScoreCell
                      score={row.opportunity}
                      metricsStatus={row.metrics_status}
                      detail={row.metrics_detail}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="cards">
        {rows.map((row) => (
          <article key={row.symbol} className="card">
            <div className="card-top">
              <span style={{ display: "inline-flex", gap: 6, alignItems: "center", fontWeight: 600 }}>
                {onSelectSymbol ? (
                  <button type="button" onClick={() => onSelectSymbol(row.symbol)}>
                    {row.symbol}
                  </button>
                ) : (
                  row.symbol
                )}
                <StatusPill status={row.ticker_status} />
              </span>
              <span className={`num ${changeDirection(row.price_change_percent_24h)}`}>
                {formatPrice(row.last_price)} ({formatPercent(row.price_change_percent_24h)})
              </span>
            </div>
            <div className="card-grid">
              <div>
                <span>Quote Vol</span>
                <span className="num">{formatCompact(row.quote_volume_24h)}</span>
              </div>
              <div>
                <span>Volatility</span>
                <span className="num">{metricText(row, (m) => m.volatility_percent)}</span>
              </div>
              <div>
                <span>ATR %</span>
                <span className="num">{metricText(row, (m) => m.atr_percent)}</span>
              </div>
              <div>
                <span>Rel Vol</span>
                <span className="num">{metricText(row, (m) => m.relative_volume)}</span>
              </div>
              <div>
                <span>Trend</span>
                <span className={`num ${trendClass(row)}`}>{row.metrics?.trend ?? NO_VALUE}</span>
              </div>
              <div>
                <span>Opportunity</span>
                <ScoreCell
                  score={row.opportunity}
                  metricsStatus={row.metrics_status}
                  detail={row.metrics_detail}
                />
              </div>
            </div>
            {row.ticker_detail ? (
              <p style={{ margin: 0, fontSize: 11, color: "var(--text-faint)" }}>{row.ticker_detail}</p>
            ) : null}
          </article>
        ))}
      </div>
    </>
  );
}
