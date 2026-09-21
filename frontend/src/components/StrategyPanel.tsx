"use client";

import { FreshnessBadge } from "@/components/Freshness";
import { formatAge } from "@/lib/format";
import type { ConditionOutcome, LeverageDecision, StrategyResult } from "@/lib/types";

/**
 * Strategy analysis panel.
 *
 * Three things this panel is careful about:
 *
 * 1. **It never reads as an instruction.** The bias is labelled ANALYSIS ONLY
 *    and NO ORDER EXECUTION in the header, and the backend's disclaimer is
 *    rendered in full rather than abbreviated.
 * 2. **It shows both sides.** Long and short conditions are always listed, so
 *    a NEUTRAL verdict shows how close each direction came instead of just
 *    saying "no signal".
 * 3. **It shows no bias at all when the analysis could not run.** A stale or
 *    unverifiable data status produces the reason, not a greyed-out guess.
 */

const BIAS_LABEL: Record<string, string> = {
  LONG_BIAS: "LONG BIAS",
  SHORT_BIAS: "SHORT BIAS",
  NEUTRAL: "NEUTRAL",
};

function biasClass(bias: string | null): string {
  if (bias === "LONG_BIAS") return "bias bias-long";
  if (bias === "SHORT_BIAS") return "bias bias-short";
  return "bias bias-neutral";
}

function ConditionList({
  title,
  conditions,
  met,
  total,
}: {
  title: string;
  conditions: ConditionOutcome[];
  met: number;
  total: number;
}) {
  return (
    <div className="cond-block">
      <div className="cond-head">
        <span>{title}</span>
        <span className="num">
          {met}/{total}
        </span>
      </div>
      <ul className="cond-list">
        {conditions.map((condition) => (
          <li key={condition.name} className={condition.satisfied ? "met" : "unmet"}>
            <span className="cond-mark" aria-hidden="true">
              {condition.satisfied ? "✓" : "✗"}
            </span>
            <span className="cond-name">{condition.name.replace(/_/g, " ")}</span>
            <span className="cond-detail">{condition.detail}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}


/**
 * The leverage constraint chain, shown in full including its null links.
 *
 * Displaying `exchange max: unknown` is the point. A panel that simply omitted
 * an unavailable constraint would let a reader assume it had been checked; a
 * panel showing the gap makes clear *why* nothing is approved.
 */
function LeverageChain({ leverage }: { leverage: LeverageDecision }) {
  const approved = leverage.approved_leverage;
  const link = (label: string, value: string | null, unknownNote: string) => (
    <div>
      <dt>{label}</dt>
      <dd className={value === null ? "num flat" : "num"}>
        {value === null ? unknownNote : `${Number(value)}x`}
      </dd>
    </div>
  );

  return (
    <div className="leverage">
      <div className="panel-head" style={{ borderTop: "1px solid var(--border)" }}>
        <h3 className="panel-title">Leverage</h3>
        <span
          className={approved === null ? "badge badge-warn" : "badge"}
          title={leverage.detail}
        >
          {leverage.outcome}
        </span>
      </div>

      <dl className="strategy-meta" style={{ padding: "10px 12px" }}>
        {link("Requested", leverage.requested_leverage, "none")}
        {link("Exchange max", leverage.exchange_max_leverage, "unknown")}
        {link("Risk max", leverage.risk_max_leverage, "unknown")}
        {link("Approved", approved, "none")}
      </dl>

      <p className="notice">
        <strong>{leverage.reason}</strong> — {leverage.detail}
        {leverage.basis ? (
          <>
            <br />
            <span style={{ color: "var(--text-faint)" }}>Candidate basis: {leverage.basis}</span>
          </>
        ) : null}
        <br />
        {leverage.note}
      </p>
    </div>
  );
}

export function StrategyPanel({ result }: { result: StrategyResult }) {
  const runnable = result.status === "READY";

  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2 className="panel-title">Strategy analysis</h2>
          <div className="strategy-name">
            {result.name} <span className="tag">v{result.version}</span>
          </div>
        </div>
        <div className="strategy-flags">
          <span className="badge">ANALYSIS ONLY</span>
          <span className="badge">NO ORDER EXECUTION</span>
        </div>
      </div>

      <div className="strategy-body">
        <div className="strategy-verdict">
          {runnable ? (
            <>
              <span className={biasClass(result.bias)}>
                {BIAS_LABEL[result.bias ?? "NEUTRAL"]}
              </span>
              <p className="strategy-detail">{result.detail}</p>
            </>
          ) : (
            <>
              <span className="bias bias-unavailable">{result.status}</span>
              <p className="strategy-detail">
                {result.detail ?? "The analysis could not be performed."}
              </p>
              <p className="strategy-detail">
                No bias is reported when the analysis cannot run.
              </p>
            </>
          )}
        </div>

        <dl className="strategy-meta">
          <div>
            <dt>Timeframe</dt>
            <dd className="num">{result.timeframe}</dd>
          </div>
          <div>
            <dt>Candles</dt>
            <dd className="num">{result.candles_used}</dd>
          </div>
          <div>
            <dt>Last candle</dt>
            <dd className="num">
              {result.last_candle_time
                ? new Date(result.last_candle_time).toLocaleString("en-GB", { hour12: false })
                : "—"}
            </dd>
          </div>
          <div>
            <dt>Data age</dt>
            <dd className="num">{formatAge(result.data_age_seconds)}</dd>
          </div>
          <div>
            <dt>Indicators</dt>
            <dd className="num">{result.indicators_used.join(", ")}</dd>
          </div>
          <div>
            <dt>Source</dt>
            <dd className="num">{result.source ?? "—"}</dd>
          </div>
        </dl>
      </div>

      {runnable ? (
        <div className="cond-grid">
          <ConditionList
            title="Long conditions"
            conditions={result.long_conditions}
            met={result.long_conditions_met}
            total={result.conditions_total}
          />
          <ConditionList
            title="Short conditions"
            conditions={result.short_conditions}
            met={result.short_conditions_met}
            total={result.conditions_total}
          />
        </div>
      ) : null}

      {result.leverage ? <LeverageChain leverage={result.leverage} /> : null}

      {result.data_status ? (
        <div className="panel-head" style={{ borderTop: "1px solid var(--border)" }}>
          <FreshnessBadge
            status={
              result.data_status === "OK"
                ? "OK"
                : result.data_status === "STALE"
                  ? "STALE"
                  : "UNAVAILABLE"
            }
            source={result.source ?? "unknown"}
            ageSeconds={result.data_age_seconds}
            label="candles"
          />
        </div>
      ) : null}

      <p className="notice">{result.disclaimer}</p>
    </section>
  );
}
