"use client";

import { FreshnessBadge } from "@/components/Freshness";
import { formatAge } from "@/lib/format";
import type { SetupScoreComponent, TradeSetup } from "@/lib/types";

/**
 * Strategy setup panel.
 *
 * The score is the most misreadable number on this screen, so the panel is
 * built around not letting it be misread:
 *
 * 1. **It is labelled for what it measures.** "Strategy Setup Score" and
 *    "rule alignment", never confidence, probability or expected return. The
 *    backend's own `meaning` string is rendered verbatim rather than
 *    paraphrased, because a UI paraphrase is exactly where a caveat goes to
 *    die.
 * 2. **Every component shows its arithmetic.** Contribution, weight and the
 *    raw measurement are all visible, so the total can be checked by hand
 *    instead of trusted.
 * 3. **A refusal is shown as a refusal.** STALE, INSUFFICIENT_DATA and
 *    NO_ACTIONABLE_SETUP each render their reason. Nothing is greyed out
 *    into looking like a weak signal, and no level is drawn when the backend
 *    declined to derive one.
 *
 * No bar, gauge or colour implies "good". A high score is a strong rule
 * match and nothing more.
 */

const STATUS_LABEL: Record<string, string> = {
  ACTIONABLE: "ACTIONABLE",
  NO_ACTIONABLE_SETUP: "NO ACTIONABLE SETUP",
  INSUFFICIENT_DATA: "INSUFFICIENT DATA",
  STALE: "STALE DATA",
  UNAVAILABLE: "UNAVAILABLE",
};

const REGIME_LABEL: Record<string, string> = {
  TREND_UP: "TREND UP",
  TREND_DOWN: "TREND DOWN",
  RANGE: "RANGE",
  HIGH_VOLATILITY: "HIGH VOLATILITY",
  LOW_VOLATILITY: "LOW VOLATILITY",
  UNKNOWN: "UNKNOWN",
};

function directionClass(direction: string): string {
  if (direction === "LONG") return "bias bias-long";
  if (direction === "SHORT") return "bias bias-short";
  return "bias bias-neutral";
}

function ComponentRow({ component }: { component: SetupScoreComponent }) {
  return (
    <li className="score-row">
      <span className="score-name">{component.name.replace(/_/g, " ")}</span>
      <span className="score-contrib num">{component.contribution}</span>
      <span className="score-weight num">×{component.weight}</span>
      <span className="score-detail">{component.detail}</span>
    </li>
  );
}

function Levels({ setup }: { setup: TradeSetup }) {
  const rr = setup.risk_reward;
  if (rr === null) return null;
  return (
    <div className="setup-levels">
      <div className="cond-head">
        <span>Risk / reward</span>
        <span className="num">{rr.risk_reward_ratio}R</span>
      </div>
      <dl className="level-grid">
        <div>
          <dt>Entry</dt>
          <dd className="num">{rr.entry_price}</dd>
        </div>
        <div>
          <dt>Stop</dt>
          <dd className="num level-stop">{rr.stop_price}</dd>
        </div>
        <div>
          <dt>Target</dt>
          <dd className="num level-target">{rr.take_profit_price}</dd>
        </div>
        <div>
          <dt>Risk / unit</dt>
          <dd className="num">{rr.risk_per_unit}</dd>
        </div>
        <div>
          <dt>Reward / unit</dt>
          <dd className="num">{rr.reward_per_unit}</dd>
        </div>
        <div>
          <dt>Stop model</dt>
          <dd>
            {rr.stop_model} · {rr.stop_distance_percent}%
          </dd>
        </div>
      </dl>
      {/* The assumption most likely to be forgotten once these numbers are
          read as if they were quotes. */}
      <p className="setup-basis">{rr.entry_basis}</p>
    </div>
  );
}

export function SetupPanel({ setup }: { setup: TradeSetup }) {
  const actionable = setup.status === "ACTIONABLE";
  const score = setup.score;

  return (
    <section className="panel setup-panel" aria-label="Strategy setup">
      <header className="panel-head">
        <div>
          <h2>Strategy Setup Score</h2>
          <p className="panel-sub">
            {setup.strategy} v{setup.strategy_version} · {setup.timeframe} · rule
            alignment, not a probability of profit
          </p>
        </div>
        <span className="tag tag-analysis">ANALYSIS ONLY</span>
      </header>

      <div className="setup-verdict">
        <span className={directionClass(setup.direction)}>
          {setup.direction === "NO_SIGNAL" ? "NO SIGNAL" : setup.direction}
        </span>
        <span className="setup-status">{STATUS_LABEL[setup.status] ?? setup.status}</span>
        {score !== null ? (
          <span className="setup-score num" title={score.meaning}>
            {score.value}
            <span className="setup-score-of">/100</span>
          </span>
        ) : null}
      </div>

      <p className="setup-detail">{setup.detail}</p>

      {setup.regime !== null ? (
        <div className="setup-regime">
          <div className="cond-head">
            <span>Market regime</span>
            <span className="num">
              {REGIME_LABEL[setup.regime.regime] ?? setup.regime.regime}
            </span>
          </div>
          <p className="regime-reason">{setup.regime.reason}</p>
          <ul className="regime-measures">
            {setup.regime.measurements.map((measurement) => (
              <li key={measurement.name}>
                <span className="cond-name">{measurement.name.replace(/_/g, " ")}</span>
                <span className="cond-detail">{measurement.detail}</span>
              </li>
            ))}
          </ul>
          <p className="regime-note">
            Volatility band {setup.regime.volatility_band}. Rule-based classification from
            published thresholds — no model is involved.
          </p>
        </div>
      ) : null}

      {actionable && score !== null ? (
        <div className="setup-score-block">
          <div className="cond-head">
            <span>Score components</span>
            <span className="num">{score.value}</span>
          </div>
          <ul className="score-list">
            {score.components.map((component) => (
              <ComponentRow key={component.name} component={component} />
            ))}
          </ul>
          {/* Verbatim from the backend. Paraphrasing this is how a score
              quietly becomes a confidence. */}
          <p className="setup-meaning">{score.meaning}</p>
        </div>
      ) : null}

      <Levels setup={setup} />

      {setup.conditions.length > 0 ? (
        <div className="cond-block">
          <div className="cond-head">
            <span>Conditions</span>
            <span className="num">
              {setup.conditions.filter((c) => c.satisfied).length}/{setup.conditions.length}
            </span>
          </div>
          <ul className="cond-list">
            {setup.conditions.map((condition) => (
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
      ) : null}

      {setup.data_status ? (
        <div className="panel-head" style={{ borderTop: "1px solid var(--border)" }}>
          <FreshnessBadge
            status={
              setup.data_status === "OK"
                ? "OK"
                : setup.data_status === "STALE"
                  ? "STALE"
                  : "UNAVAILABLE"
            }
            source={setup.data_source ?? "unknown"}
            ageSeconds={setup.data_age_seconds}
            label="candles"
          />
          <span className="panel-sub">
            {setup.candles_used} candles · {formatAge(setup.data_age_seconds)}
          </span>
        </div>
      ) : null}

      <p className="notice">{setup.disclaimer}</p>
    </section>
  );
}
