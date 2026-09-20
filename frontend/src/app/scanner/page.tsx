"use client";

import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import { EmptyState, ErrorState, LoadingState } from "@/components/DataState";
import { FreshnessBadge } from "@/components/Freshness";
import { ScannerTable } from "@/components/ScannerTable";
import { getScan } from "@/lib/api";
import { formatClock } from "@/lib/format";
import { METRIC_SORT_FIELDS, TIMEFRAMES, type ScannerSortField, type Timeframe } from "@/lib/types";
import { useApiResource } from "@/lib/useApiResource";

/**
 * Market scanner.
 *
 * Polls slowly (30s): a scan is the most expensive request the backend serves,
 * and the underlying snapshot is cached for a few seconds anyway. Paused while
 * the tab is hidden.
 */

const SCAN_POLL_MS = 30_000;
const PAGE_SIZE = 25;
const SYMBOL_STORAGE_KEY = "aetheris.markets.symbol";

export default function ScannerPage() {
  const router = useRouter();
  const [search, setSearch] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [sortBy, setSortBy] = useState<ScannerSortField>("quote_volume_24h");
  const [direction, setDirection] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(1);
  const [timeframe, setTimeframe] = useState<Timeframe>("1h");
  const [includeMetrics, setIncludeMetrics] = useState(false);

  const sortsByMetric = (METRIC_SORT_FIELDS as readonly string[]).includes(sortBy);

  const scan = useApiResource(
    (signal) =>
      getScan(
        {
          search: submitted || undefined,
          sort: sortBy,
          direction,
          page,
          pageSize: PAGE_SIZE,
          timeframe,
          includeMetrics: includeMetrics || sortsByMetric,
        },
        signal,
      ),
    [submitted, sortBy, direction, page, timeframe, includeMetrics, sortsByMetric],
    { pollMs: SCAN_POLL_MS },
  );

  const onSort = useCallback(
    (field: ScannerSortField) => {
      if (field === sortBy) {
        setDirection((d) => (d === "desc" ? "asc" : "desc"));
      } else {
        setSortBy(field);
        setDirection("desc");
      }
      setPage(1);
    },
    [sortBy],
  );

  const openSymbol = useCallback(
    (symbol: string) => {
      try {
        window.localStorage.setItem(SYMBOL_STORAGE_KEY, symbol);
      } catch {
        /* preference only */
      }
      router.push("/markets");
    },
    [router],
  );

  const data = scan.state.kind === "success" ? scan.state.data : null;
  const totalPages = data ? Math.max(1, Math.ceil(data.total_rows / data.page_size)) : 1;

  return (
    <section className="panel">
      <div className="panel-head">
        <h1 className="panel-title">Scanner</h1>
        <div className="controls">
          <form
            onSubmit={(event) => {
              event.preventDefault();
              setSubmitted(search.trim());
              setPage(1);
            }}
          >
            <input
              className="input"
              type="search"
              placeholder="Filter by symbol or asset"
              value={search}
              aria-label="Filter instruments"
              onChange={(event) => setSearch(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  setSearch("");
                  setSubmitted("");
                  setPage(1);
                }
              }}
            />
          </form>
          <div className="segmented" role="group" aria-label="Metric timeframe">
            {TIMEFRAMES.map((frame) => (
              <button
                key={frame}
                type="button"
                aria-pressed={frame === timeframe}
                onClick={() => {
                  setTimeframe(frame);
                  setPage(1);
                }}
              >
                {frame}
              </button>
            ))}
          </div>
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12 }}>
            <input
              type="checkbox"
              checked={includeMetrics || sortsByMetric}
              disabled={sortsByMetric}
              onChange={(event) => {
                setIncludeMetrics(event.target.checked);
                setPage(1);
              }}
            />
            Candle metrics
          </label>
        </div>
      </div>

      {data ? (
        <div className="panel-head" style={{ borderBottom: "1px solid var(--border)" }}>
          <FreshnessBadge
            status={data.ticker_status}
            source={data.ticker_source}
            ageSeconds={data.ticker_age_seconds}
            label="snapshot"
          />
          <span className="freshness">
            <span>{data.universe_size} eligible instruments</span>
            <span aria-hidden="true">·</span>
            <span>scanned {formatClock(data.scanned_at)}</span>
          </span>
        </div>
      ) : null}

      {scan.state.kind === "loading" ? (
        <LoadingState label="Scanning market" />
      ) : scan.state.kind === "error" ? (
        <ErrorState error={scan.state.error} onRetry={scan.refresh} />
      ) : data && data.rows.length === 0 ? (
        <EmptyState
          message={
            submitted
              ? `No instrument matches "${submitted}".`
              : "No instruments matched the current filters."
          }
          detail={`${data.universe_size} instruments were discovered, so the feed is working — the filters simply exclude them all.`}
        />
      ) : data ? (
        <>
          <ScannerTable
            rows={data.rows}
            sortBy={data.sort_by}
            direction={direction}
            onSort={onSort}
            onSelectSymbol={openSymbol}
          />

          {data.ranking_scope === "LIQUIDITY_POOL" ? (
            <p className="notice">
              <strong>Ranking scope: liquidity pool.</strong> Candle-derived metrics cost
              one request per instrument, so they were computed for the{" "}
              {data.candidate_pool_size} most liquid instruments and this ordering covers
              that pool — not all {data.universe_size} eligible instruments.
            </p>
          ) : null}

          <p className="notice">
            <strong>Market Opportunity Score</strong> is a deterministic ranking metric
            over four present-tense measurements (relative volume, volatility, momentum,
            trend consistency). It is <strong>not</strong> a probability of profit,
            expected return, win rate, trading signal or prediction of future price. Hover
            a score to see every component&apos;s contribution.
          </p>

          <div className="pager">
            <span>
              Page {data.page} of {totalPages} · {data.total_rows} rows
            </span>
            <div className="pager-buttons">
              <button type="button" disabled={data.page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
                Previous
              </button>
              <button
                type="button"
                disabled={data.page >= totalPages}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </button>
            </div>
          </div>
        </>
      ) : null}
    </section>
  );
}
