"use client";

import { useCallback, useEffect, useState } from "react";
import { CandleChart } from "@/components/CandleChart";
import { ErrorState, LoadingState, UnavailableState } from "@/components/DataState";
import { FreshnessBadge } from "@/components/Freshness";
import { MarketHeader } from "@/components/MarketHeader";
import { SymbolSearch } from "@/components/SymbolSearch";
import { getKlines, getMarketStatus, getTicker } from "@/lib/api";
import { formatClock } from "@/lib/format";
import { TIMEFRAMES, type Timeframe } from "@/lib/types";
import { useApiResource } from "@/lib/useApiResource";

/**
 * Markets terminal.
 *
 * Polling intervals are deliberately unhurried. A ticker refresh every 12s and
 * candles every 45s is enough for a human reading a chart, and the backend
 * caches beneath that anyway; polling harder would spend the venue rate-limit
 * budget on numbers nobody is watching. Both pause when the tab is hidden.
 */

const TICKER_POLL_MS = 12_000;
const KLINE_POLL_MS = 45_000;
const CANDLE_LIMIT = 200;

/** The instrument shown before the user picks one. */
const INITIAL_SYMBOL = "BTCUSDT";
const SYMBOL_STORAGE_KEY = "aetheris.markets.symbol";
const TIMEFRAME_STORAGE_KEY = "aetheris.markets.timeframe";

function isTimeframe(value: string | null): value is Timeframe {
  return value !== null && (TIMEFRAMES as readonly string[]).includes(value);
}

export default function MarketsPage() {
  const [symbol, setSymbol] = useState(INITIAL_SYMBOL);
  const [timeframe, setTimeframe] = useState<Timeframe>("1h");

  // Restored after mount rather than during render: reading localStorage while
  // rendering would make the server and client markup disagree.
  useEffect(() => {
    try {
      const storedSymbol = window.localStorage.getItem(SYMBOL_STORAGE_KEY);
      if (storedSymbol && /^[A-Z0-9_]{1,32}$/.test(storedSymbol)) setSymbol(storedSymbol);
      const storedTimeframe = window.localStorage.getItem(TIMEFRAME_STORAGE_KEY);
      if (isTimeframe(storedTimeframe)) setTimeframe(storedTimeframe);
    } catch {
      // Private browsing or blocked storage: the defaults are fine.
    }
  }, []);

  const selectSymbol = useCallback((next: string) => {
    setSymbol(next);
    try {
      window.localStorage.setItem(SYMBOL_STORAGE_KEY, next);
    } catch {
      /* preference only; never required */
    }
  }, []);

  const selectTimeframe = useCallback((next: Timeframe) => {
    setTimeframe(next);
    try {
      window.localStorage.setItem(TIMEFRAME_STORAGE_KEY, next);
    } catch {
      /* preference only; never required */
    }
  }, []);

  const ticker = useApiResource(
    (signal) => getTicker(symbol, signal),
    [symbol],
    { pollMs: TICKER_POLL_MS },
  );

  const klines = useApiResource(
    (signal) => getKlines(symbol, timeframe, CANDLE_LIMIT, signal),
    [symbol, timeframe],
    { pollMs: KLINE_POLL_MS },
  );

  const connection = useApiResource((signal) => getMarketStatus(signal), [], {
    pollMs: 60_000,
  });

  return (
    <div className="grid-2">
      <section className="panel">
        <div className="panel-head">
          <div className="controls" style={{ flex: 1 }}>
            <SymbolSearch value={symbol} onSelect={selectSymbol} />
            <div className="segmented" role="group" aria-label="Timeframe">
              {TIMEFRAMES.map((frame) => (
                <button
                  key={frame}
                  type="button"
                  aria-pressed={frame === timeframe}
                  onClick={() => selectTimeframe(frame)}
                >
                  {frame}
                </button>
              ))}
            </div>
          </div>
          {connection.state.kind === "success" ? (
            <span className="freshness">
              <span
                className={
                  connection.state.data.connection_status === "CONNECTED"
                    ? "dot dot-ok"
                    : connection.state.data.connection_status === "DEGRADED"
                      ? "dot dot-stale"
                      : "dot dot-unknown"
                }
                aria-hidden="true"
              />
              <span>{connection.state.data.connection_status}</span>
              <span aria-hidden="true">·</span>
              <span>{connection.state.data.exchange}</span>
              {connection.state.data.eligible_symbols !== null ? (
                <>
                  <span aria-hidden="true">·</span>
                  <span>{connection.state.data.eligible_symbols} eligible</span>
                </>
              ) : null}
            </span>
          ) : null}
        </div>

        {ticker.state.kind === "loading" ? (
          <LoadingState label="Loading ticker" />
        ) : ticker.state.kind === "error" ? (
          <ErrorState error={ticker.state.error} onRetry={ticker.refresh} />
        ) : (
          <MarketHeader symbol={symbol} observation={ticker.state.data} />
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">
            {symbol} · {timeframe} · OHLCV
          </h2>
          {klines.state.kind === "success" ? (
            <FreshnessBadge
              status={klines.state.data.status}
              source={klines.state.data.source}
              ageSeconds={klines.state.data.age_seconds}
              label="candles"
            />
          ) : null}
        </div>

        {klines.state.kind === "loading" ? (
          <LoadingState label="Loading candles" />
        ) : klines.state.kind === "error" ? (
          <ErrorState error={klines.state.error} onRetry={klines.refresh} />
        ) : klines.state.data.value === null ? (
          <UnavailableState
            status={klines.state.data.status}
            detail={klines.state.data.detail}
            onRetry={klines.refresh}
          />
        ) : (
          <>
            <CandleChart
              candles={klines.state.data.value.candles}
              timeframe={klines.state.data.value.timeframe}
              symbol={symbol}
            />
            <div className="notice">
              {klines.state.data.value.candles.length} candles · last update{" "}
              {formatClock(klines.state.data.received_ts)} · source{" "}
              <strong>{klines.state.data.source}</strong>
              {" · "}
              Indicators (SMA, EMA, RSI, MACD, Bollinger, VWAP, ADX) and Smart Money
              Concepts overlays are <strong>NOT AVAILABLE</strong> in this build — no
              backend calculation exists for them yet (phase 4).
            </div>
          </>
        )}
      </section>
    </div>
  );
}
