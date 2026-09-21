"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CandleChart } from "@/components/CandleChart";
import { ErrorState, LoadingState, UnavailableState } from "@/components/DataState";
import { FreshnessBadge } from "@/components/Freshness";
import { IndicatorSelector } from "@/components/IndicatorSelector";
import { MarketHeader } from "@/components/MarketHeader";
import { OscillatorPane } from "@/components/OscillatorPane";
import { StrategyPanel } from "@/components/StrategyPanel";
import { SymbolSearch } from "@/components/SymbolSearch";
import {
  getIndicatorCatalogue,
  getIndicators,
  getKlines,
  getMarketStatus,
  getStrategy,
  getTicker,
} from "@/lib/api";
import { formatClock } from "@/lib/format";
import { alignIndicator } from "@/lib/indicators";
import { TIMEFRAMES, type Timeframe } from "@/lib/types";
import { useApiResource } from "@/lib/useApiResource";

/**
 * Markets terminal.
 *
 * Polling intervals are deliberately unhurried. A ticker refresh every 12s,
 * candles and indicators every 45s, and the strategy every 60s is enough for a
 * human reading a chart, and the backend caches beneath that anyway; polling
 * harder would spend the venue rate-limit budget on numbers nobody is
 * watching. Everything pauses when the tab is hidden.
 *
 * Indicators are fetched in one request alongside the candles rather than one
 * request per line, and the selection is capped by the backend's own published
 * `max_per_request`.
 */

const TICKER_POLL_MS = 12_000;
const KLINE_POLL_MS = 45_000;
const STRATEGY_POLL_MS = 60_000;
const CANDLE_LIMIT = 200;

const INITIAL_SYMBOL = "BTCUSDT";
const SYMBOL_STORAGE_KEY = "aetheris.markets.symbol";
const TIMEFRAME_STORAGE_KEY = "aetheris.markets.timeframe";
const INDICATORS_STORAGE_KEY = "aetheris.markets.indicators";

const DEFAULT_INDICATORS = ["ema", "rsi", "macd"];

function isTimeframe(value: string | null): value is Timeframe {
  return value !== null && (TIMEFRAMES as readonly string[]).includes(value);
}

export default function MarketsPage() {
  const [symbol, setSymbol] = useState(INITIAL_SYMBOL);
  const [timeframe, setTimeframe] = useState<Timeframe>("1h");
  const [selected, setSelected] = useState<string[]>(DEFAULT_INDICATORS);

  // Restored after mount rather than during render: reading localStorage while
  // rendering would make the server and client markup disagree.
  useEffect(() => {
    try {
      const storedSymbol = window.localStorage.getItem(SYMBOL_STORAGE_KEY);
      if (storedSymbol && /^[A-Z0-9_]{1,32}$/.test(storedSymbol)) setSymbol(storedSymbol);
      const storedTimeframe = window.localStorage.getItem(TIMEFRAME_STORAGE_KEY);
      if (isTimeframe(storedTimeframe)) setTimeframe(storedTimeframe);
      const storedIndicators = window.localStorage.getItem(INDICATORS_STORAGE_KEY);
      if (storedIndicators) {
        const parsed: unknown = JSON.parse(storedIndicators);
        if (Array.isArray(parsed) && parsed.every((k) => typeof k === "string")) {
          setSelected(parsed as string[]);
        }
      }
    } catch {
      // Private browsing, blocked storage or corrupt JSON: defaults are fine.
    }
  }, []);

  const remember = useCallback((key: string, value: string) => {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      /* preference only; never required */
    }
  }, []);

  const selectSymbol = useCallback(
    (next: string) => {
      setSymbol(next);
      remember(SYMBOL_STORAGE_KEY, next);
    },
    [remember],
  );

  const selectTimeframe = useCallback(
    (next: Timeframe) => {
      setTimeframe(next);
      remember(TIMEFRAME_STORAGE_KEY, next);
    },
    [remember],
  );

  const catalogue = useApiResource((signal) => getIndicatorCatalogue(signal), []);
  const maxIndicators =
    catalogue.state.kind === "success" ? catalogue.state.data.max_per_request : 8;

  const toggleIndicator = useCallback(
    (key: string) => {
      setSelected((current) => {
        const next = current.includes(key)
          ? current.filter((entry) => entry !== key)
          : current.length >= maxIndicators
            ? current
            : [...current, key];
        remember(INDICATORS_STORAGE_KEY, JSON.stringify(next));
        return next;
      });
    },
    [maxIndicators, remember],
  );

  const ticker = useApiResource((signal) => getTicker(symbol, signal), [symbol], {
    pollMs: TICKER_POLL_MS,
  });

  const klines = useApiResource(
    (signal) => getKlines(symbol, timeframe, CANDLE_LIMIT, signal),
    [symbol, timeframe],
    { pollMs: KLINE_POLL_MS },
  );

  // Joined so the request key changes whenever the selection does.
  const selectionKey = selected.join(",");
  const indicators = useApiResource(
    (signal) =>
      getIndicators(
        symbol,
        timeframe,
        selected,
        { limit: CANDLE_LIMIT, seriesPoints: CANDLE_LIMIT },
        signal,
      ),
    [symbol, timeframe, selectionKey],
    { pollMs: KLINE_POLL_MS, enabled: selected.length > 0 },
  );

  const strategy = useApiResource(
    (signal) => getStrategy(symbol, timeframe, signal),
    [symbol, timeframe],
    { pollMs: STRATEGY_POLL_MS },
  );

  const connection = useApiResource((signal) => getMarketStatus(signal), [], {
    pollMs: 60_000,
  });

  const candles = klines.state.kind === "success" ? klines.state.data.value?.candles : undefined;
  const indicatorResults =
    indicators.state.kind === "success" ? indicators.state.data.indicators : [];

  const overlays = useMemo(() => {
    if (!candles) return [];
    return indicatorResults
      .filter((result) => result.kind === "OVERLAY")
      .flatMap((result) => alignIndicator(candles, result));
  }, [candles, indicatorResults]);

  const oscillators = indicatorResults.filter((result) => result.kind === "OSCILLATOR");

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

        {catalogue.state.kind === "success" ? (
          <IndicatorSelector
            catalogue={catalogue.state.data.indicators}
            selected={selected}
            maxSelected={maxIndicators}
            onToggle={toggleIndicator}
          />
        ) : catalogue.state.kind === "error" ? (
          <div className="notice">
            Indicator catalogue unavailable ({catalogue.state.error.code}); no indicator can
            be selected until the backend answers.
          </div>
        ) : null}

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
              overlays={overlays}
            />

            {indicators.state.kind === "error" ? (
              <div className="notice">
                Indicators unavailable ({indicators.state.error.code}):{" "}
                {indicators.state.error.message}
              </div>
            ) : null}

            {oscillators.map((result) => (
              <OscillatorPane
                key={result.indicator}
                candles={candles ?? []}
                result={result}
              />
            ))}

            <div className="notice">
              {klines.state.data.value.candles.length} candles · last update{" "}
              {formatClock(klines.state.data.received_ts)} · source{" "}
              <strong>{klines.state.data.source}</strong>
              {" · "}
              Smart Money Concepts overlays (BOS, CHoCH, FVG, order blocks, liquidity)
              remain <strong>NOT AVAILABLE</strong> — no backend calculation exists for
              them yet.
            </div>
          </>
        )}
      </section>

      {strategy.state.kind === "loading" ? (
        <section className="panel">
          <LoadingState label="Evaluating strategy" />
        </section>
      ) : strategy.state.kind === "error" ? (
        <section className="panel">
          <ErrorState error={strategy.state.error} onRetry={strategy.refresh} />
        </section>
      ) : (
        <StrategyPanel result={strategy.state.data} />
      )}
    </div>
  );
}
