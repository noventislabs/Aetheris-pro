"use client";

import { FreshnessBadge } from "@/components/Freshness";
import { changeDirection, formatCompact, formatPrice, formatPercent, NO_VALUE } from "@/lib/format";
import type { ObservationEnvelope, Ticker } from "@/lib/types";

/**
 * Selected-instrument header.
 *
 * When the observation is not OK the numbers are absent, not stale copies: the
 * backend withholds the value and so does this. The header still renders, so
 * the symbol and the reason stay on screen rather than the panel vanishing.
 */

function Stat({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${className ?? ""}`}>{value}</div>
    </div>
  );
}

export function MarketHeader({
  symbol,
  observation,
}: {
  symbol: string;
  observation: ObservationEnvelope<Ticker>;
}) {
  const ticker = observation.value;
  const direction = changeDirection(ticker?.price_change_percent_24h);

  return (
    <div>
      <div className="panel-head">
        <div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
            <h2 style={{ margin: 0, fontSize: 18, letterSpacing: "0.02em" }}>{symbol}</h2>
            <span className={`price-main ${direction}`}>
              {ticker ? formatPrice(ticker.last_price) : NO_VALUE}
            </span>
            <span className={`num ${direction}`}>
              {formatPercent(ticker?.price_change_percent_24h)}
            </span>
          </div>
          {!ticker && observation.detail ? (
            <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--warn)" }}>
              {observation.detail}
            </p>
          ) : null}
        </div>
        <FreshnessBadge
          status={observation.status}
          source={observation.source}
          ageSeconds={observation.age_seconds}
          label="ticker"
        />
      </div>

      <div className="stats">
        <Stat label="Bid" value={ticker ? formatPrice(ticker.bid_price) : NO_VALUE} />
        <Stat label="Ask" value={ticker ? formatPrice(ticker.ask_price) : NO_VALUE} />
        <Stat label="24h High" value={ticker ? formatPrice(ticker.high_24h) : NO_VALUE} />
        <Stat label="24h Low" value={ticker ? formatPrice(ticker.low_24h) : NO_VALUE} />
        <Stat
          label="24h Change"
          value={ticker ? formatPrice(ticker.price_change_24h) : NO_VALUE}
          className={direction}
        />
        <Stat label="Volume" value={ticker ? formatCompact(ticker.volume_24h) : NO_VALUE} />
        <Stat
          label="Quote Volume"
          value={ticker ? formatCompact(ticker.quote_volume_24h) : NO_VALUE}
        />
      </div>
    </div>
  );
}
